"""PaperEngine.fork(): a copy must carry on exactly like the original, and must share nothing with it."""

import dataclasses
import unittest
from dataclasses import replace

from sniper.paper.engine import PaperEngine, Strategy
from sniper.pump import synth

MARKET = synth.generate(11, 90, 1_000_000)

STRATEGIES = {
    "default": Strategy(latency_ms=300),
    "trail+ladder": Strategy(latency_ms=300, trailing_stop_pct=12, ladder=((15.0, 40.0), (40.0, 30.0)), take_profit_pct=80),
    "confirm+smart": Strategy(latency_ms=300, entry_mode="confirm", confirm_gain_pct=6, exit_mode="smart"),
    "filters": Strategy(latency_ms=300, min_trades=2, max_entry_gain_pct=60, block_collapsed_creators=True, max_creator_launches=2, min_liquidity_sol=0.5),
    "no-scan": Strategy(latency_ms=0, require_scan=False, tx_cost_sol=0.001),
    "random-size": Strategy(latency_ms=300, size_mode="random", size_sol=0.2, size_caution_sol=0.05, max_open=6),
    "compounding": Strategy(latency_ms=300, auto_compound=True, size_frac=0.1, max_open=5),
}


def outcome(engine: PaperEngine):
    r = engine.finish()
    return [dataclasses.asdict(c) for c in r.closed], r.final_equity, dict(r.skipped), r.equity_curve


def state_summary(e: PaperEngine):
    return (e.cash, e.last_t, [dataclasses.asdict(c) for c in e.closed], {m: dataclasses.asdict(p) for m, p in e.positions.items()},
            dict(e.pending), dict(e.watch), dict(e.skipped), sorted(e.known_ruggers), dict(e.creator_sold), len(e.tapes),
            {m: (list(t.ts), list(t.vs), t.peak_price) for m, t in e.tapes.items()}, {c: list(v) for c, v in e.creator_launches.items()},
            list(e.equity_curve))


seen: dict[str, int] = {}


class ForkContinuesIdentically(unittest.TestCase):
    def test_a_fork_made_part_way_through_finishes_exactly_like_the_original(self):
        for name, strat in STRATEGIES.items():
            for frac in (0.0, 0.2, 0.5, 0.8):
                k = int(len(MARKET) * frac)
                with self.subTest(strategy=name, split=frac):
                    original = PaperEngine(strat, 1.0)
                    for ev in MARKET[:k]:
                        original.on_event(ev)
                    twin = original.fork()
                    for ev in MARKET[k:]:
                        original.on_event(ev)
                        twin.on_event(ev)
                    self.assertEqual(outcome(twin), outcome(original))

    def test_a_fork_made_at_the_awkward_moments_also_finishes_identically(self):
        """Splits chosen where an order is waiting to fill, a confirmation is being watched, a position is open, or a creator has sold."""
        for name, strat in STRATEGIES.items():
            probe, moments = PaperEngine(strat, 1.0), {"pending": [], "watch": [], "positions": [], "creator_sold": [], "exiting": []}
            for i, ev in enumerate(MARKET):
                probe.on_event(ev)
                for key, there in (("pending", probe.pending), ("watch", probe.watch), ("positions", probe.positions), ("creator_sold", probe.creator_sold),
                                   ("exiting", [p for p in probe.positions.values() if p.exit_ready_ms is not None])):
                    if there and len(moments[key]) < 3 and (not moments[key] or i - moments[key][-1] > 25):
                        moments[key].append(i + 1)
            for key, splits in moments.items():
                for k in splits:
                    with self.subTest(strategy=name, moment=key, split=k):
                        original = PaperEngine(strat, 1.0)
                        for ev in MARKET[:k]:
                            original.on_event(ev)
                        twin = original.fork()
                        for ev in MARKET[k:]:
                            original.on_event(ev)
                            twin.on_event(ev)
                        self.assertEqual(outcome(twin), outcome(original))
                        seen[key] = seen.get(key, 0) + 1
        for key in ("pending", "positions", "creator_sold", "exiting", "watch"):
            self.assertGreater(seen.get(key, 0), 0, f"no fork was ever made while '{key}' was non-empty")

    def test_the_scenarios_really_trade_and_have_positions_open_at_the_split(self):
        """Guard against a vacuous pass: forking has to happen while there is something to copy."""
        traded, open_at_split = 0, 0
        for strat in STRATEGIES.values():
            e = PaperEngine(strat, 1.0)
            for ev in MARKET[: len(MARKET) // 2]:
                e.on_event(ev)
                open_at_split = max(open_at_split, len(e.positions) + len(e.pending) + len(e.watch))
            for ev in MARKET[len(MARKET) // 2:]:
                e.on_event(ev)
            traded += len(e.finish().closed)
        self.assertGreater(traded, 40)
        self.assertGreater(open_at_split, 0)


class ForkIsIndependent(unittest.TestCase):
    def half(self, strat=STRATEGIES["trail+ladder"]):
        e = PaperEngine(strat, 1.0)
        for ev in MARKET[: len(MARKET) // 2]:
            e.on_event(ev)
        return e

    def test_feeding_the_copy_does_not_touch_the_original(self):
        original = self.half()
        before = state_summary(original)
        twin = original.fork()
        for ev in MARKET[len(MARKET) // 2:]:
            twin.on_event(ev)
        twin.finish()
        self.assertEqual(state_summary(original), before)

    def test_feeding_the_original_does_not_touch_the_copy(self):
        original = self.half()
        twin = original.fork()
        before = state_summary(twin)
        for ev in MARKET[len(MARKET) // 2:]:
            original.on_event(ev)
        self.assertEqual(state_summary(twin), before)

    def test_no_mutable_container_is_shared(self):
        original = self.half()
        twin = original.fork()
        for name in ("tapes", "curve", "launch", "scans", "pending", "watch", "pending_state", "creator_sold", "known_ruggers",
                     "creator_launches", "positions", "closed", "equity_curve", "skipped", "rng"):
            self.assertIsNot(getattr(twin, name), getattr(original, name), name)
        self.assertTrue(original.tapes)
        for m, tape in original.tapes.items():
            self.assertIsNot(twin.tapes[m], tape)
            self.assertIsNot(twin.tapes[m].ts, tape.ts)
        for m, pos in original.positions.items():
            self.assertIsNot(twin.positions[m], pos)
        for c, launches in original.creator_launches.items():
            self.assertIsNot(twin.creator_launches[c], launches)

    def test_a_copy_does_not_make_notes(self):
        notes = []
        original = PaperEngine(STRATEGIES["default"], 1.0, on_note=lambda *a: notes.append(a))
        for ev in MARKET[:30]:
            original.on_event(ev)
        before = len(notes)
        twin = original.fork()
        for ev in MARKET[30:]:
            twin.on_event(ev)
        self.assertIsNone(twin.on_note)
        self.assertEqual(len(notes), before)


class ForkMidScenario(unittest.TestCase):
    """Hand-built cases for state that random markets rarely exercise."""

    def run_split(self, strat, events, k):
        original = PaperEngine(strat, 1.0)
        for ev in events[:k]:
            original.on_event(ev)
        twin = original.fork()
        for ev in events[k:]:
            original.on_event(ev)
            twin.on_event(ev)
        return original, twin

    def test_an_order_waiting_to_fill_still_checks_slippage_against_the_decision_price(self):
        sim = synth.Sim("M1", "C1", 1000)
        events = [sim.create(), sim.scan(1100), sim.buy(1300, 1.0), sim.buy(1500, 5.0), sim.buy(1700, 0.01)]
        strat = Strategy(latency_ms=500, tx_cost_sol=0.0, slippage_pct=5)
        original, twin = self.run_split(strat, events, 3)  # decided at 1100, not filled yet
        self.assertEqual(original.skipped["slippage"], 1)
        self.assertEqual(twin.skipped["slippage"], 1)
        self.assertEqual((len(original.positions), len(twin.positions)), (0, 0))

    def test_a_creator_who_sells_in_two_parts_is_still_caught_when_the_fork_is_between_them(self):
        sim = synth.Sim("M1", "RUGGER", 1000)
        launch = sim.create(dev_sol=2.0)
        events = [launch, sim.scan(1100), sim.sell(1300, launch.dev_tokens * 0.3, "RUGGER"), sim.sell(1400, launch.dev_tokens * 0.3, "RUGGER")]
        original, twin = self.run_split(Strategy(latency_ms=0, tx_cost_sol=0.0), events, 3)  # first 30% sold, second not yet
        self.assertEqual(original.known_ruggers, {"RUGGER"})
        self.assertEqual(twin.known_ruggers, {"RUGGER"})


class ForkWithNewSettings(unittest.TestCase):
    def test_the_copy_uses_the_strategy_it_was_given_and_the_original_keeps_its_own(self):
        base = replace(STRATEGIES["default"], take_profit_pct=100, stop_loss_pct=60)
        original = PaperEngine(base, 1.0)
        for ev in MARKET[:40]:
            original.on_event(ev)
        quick = original.fork(replace(base, take_profit_pct=6, stop_loss_pct=6))
        self.assertIs(original.s, base)
        self.assertEqual(quick.s.take_profit_pct, 6)
        for ev in MARKET[40:]:
            original.on_event(ev)
            quick.on_event(ev)
        a, b = outcome(original), outcome(quick)
        self.assertNotEqual(a[0], b[0])
        reasons = {c["reason"] for c in b[0]}
        self.assertIn("take_profit", reasons)


if __name__ == "__main__":
    unittest.main()
