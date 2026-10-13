import contextlib
import io
import os
import tempfile
import unittest
from dataclasses import replace

from sniper.paper.__main__ import baseline, main as paper_main, split_events
from sniper.paper.engine import PaperEngine, Strategy, replay
from sniper.paper.stats import bootstrap_ci, max_drawdown_pct, summarise
from sniper.pump.events import NewToken, write_jsonl

from .sim import Sim

# Zero latency and zero tx cost keep the arithmetic exact; individual tests turn them back on.
S = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1)


def winner(sim: Sim, t0: int, scan="PASS"):
    """Launch, enter on a quiet moment, then a big pump that clears the take-profit."""
    return [
        sim.create(),
        sim.scan(t0 + 100, scan),
        sim.buy(t0 + 200, 1.0),
        sim.buy(t0 + 300, 12.0),
        sim.buy(t0 + 400, 0.5),
    ]


def loser(sim: Sim, t0: int, scan="PASS"):
    """Early pump we enter into, then a collapse that hits the stop-loss."""
    return [
        sim.create(),
        sim.buy(t0 + 50, 10.0),
        sim.scan(t0 + 100, scan),
        sim.buy(t0 + 200, 0.2),
        sim.sell(t0 + 300, 2.0e8, "DUMPER"),
        sim.buy(t0 + 400, 0.1),
    ]


class Exits(unittest.TestCase):
    def test_take_profit_fills_at_the_state_before_the_next_event(self):
        sim = Sim()
        events = winner(sim, 1000)
        r = replay(events, S)
        (t,) = r.closed
        self.assertEqual(t.reason, "take_profit")
        self.assertGreaterEqual(t.ret_pct, 30)
        self.assertEqual(t.exit_t, 1300)
        # exit price is the state after the 12 SOL buy, not after the final 0.5 SOL buy
        self.assertAlmostEqual(t.exit_mcap, events[3].v_sol / events[3].v_tokens * 1e9)

    def test_stop_loss(self):
        (t,) = replay(loser(Sim(), 1000), S).closed
        self.assertEqual(t.reason, "stop_loss")
        self.assertLessEqual(t.ret_pct, -20)

    def test_creator_sell_exits_even_while_price_is_fine(self):
        sim = Sim()
        launch = sim.create(dev_sol=1.0)
        events = [launch, sim.scan(1100), sim.buy(1200, 0.2), sim.sell(1300, launch.dev_tokens * 0.1, "CREATOR"), sim.buy(1400, 0.1)]
        (t,) = replay(events, S).closed
        self.assertEqual(t.reason, "creator_sold")

    def test_creator_sell_rule_can_be_disabled(self):
        sim = Sim()
        launch = sim.create(dev_sol=1.0)
        events = [launch, sim.scan(1100), sim.buy(1200, 0.2), sim.sell(1300, launch.dev_tokens * 0.1, "CREATOR"), sim.buy(1400, 0.1)]
        (t,) = replay(events, replace(S, exit_on_creator_sell=False)).closed
        self.assertEqual(t.reason, "end_of_data")

    def test_timeout_exits_at_the_scheduled_time_not_the_next_event_time(self):
        sim = Sim()
        events = [sim.create(), sim.scan(1100), sim.buy(1200, 0.2), sim.buy(3000, 0.2)]
        (t,) = replay(events, replace(S, max_hold_s=1.0)).closed
        self.assertEqual((t.reason, t.exit_t), ("timeout", 2100))

    def test_open_position_is_closed_at_end_of_data(self):
        sim = Sim()
        r = replay([sim.create(), sim.scan(1100), sim.buy(1200, 0.2)], S)
        self.assertEqual([t.reason for t in r.closed], ["end_of_data"])


class Costs(unittest.TestCase):
    def test_pnl_includes_fees_and_both_tx_costs(self):
        events = winner(Sim(), 1000)
        free = replay(events, S).closed[0]
        paid = replay(events, replace(S, tx_cost_sol=0.01)).closed[0]
        self.assertAlmostEqual(free.pnl_sol - paid.pnl_sol, 0.02, places=9)

    def test_round_trip_at_flat_price_loses_about_two_percent(self):
        sim = Sim()
        r = replay([sim.create(), sim.scan(1100), sim.buy(1200, 0.01)], S)
        self.assertTrue(-3.0 < r.closed[0].ret_pct < -1.5, r.closed[0].ret_pct)

    def test_bankroll_equals_start_plus_pnl(self):
        sim1, sim2 = Sim("A"), Sim("B")
        r = replay(winner(sim1, 1000) + loser(sim2, 5000), replace(S, tx_cost_sol=0.003))
        self.assertAlmostEqual(r.final_equity, 1.0 + sum(t.pnl_sol for t in r.closed), places=9)


class Latency(unittest.TestCase):
    def test_price_moves_while_the_order_is_in_flight(self):
        sim = Sim()
        events = [sim.create(), sim.scan(1100), sim.buy(1300, 1.0), sim.buy(1500, 5.0), sim.buy(1700, 0.01)]
        eng = PaperEngine(replace(S, latency_ms=500, take_profit_pct=1e9))
        for ev in events[:4]:
            eng.on_event(ev)
        self.assertEqual(eng.positions, {})  # ready at 1600, not yet filled
        eng.on_event(events[4])
        pos = eng.positions["MINT1"]
        self.assertEqual(pos.entry_t, 1600)
        self.assertAlmostEqual(pos.entry_mcap, events[3].v_sol / events[3].v_tokens * 1e9)  # after the 5 SOL buy


    def test_exit_slips_when_price_reverses_during_the_delay(self):
        sim = Sim()
        events = [
            sim.create(), sim.scan(1100),
            sim.buy(1650, 0.01),             # entry fills at 1600 (scan 1100 + 500ms latency) at launch price
            sim.buy(1700, 12.0),             # pump: take-profit triggers here, exit ready at 2200
            sim.sell(1900, 2.0e8, "OTHER"),  # price reverses while the exit is in flight
            sim.buy(2300, 0.01),
        ]
        (t,) = replay(events, replace(S, latency_ms=500)).closed
        self.assertEqual((t.reason, t.exit_t), ("take_profit", 2200))
        crash = events[4]
        self.assertAlmostEqual(t.exit_mcap, crash.v_sol / crash.v_tokens * 1e9)
        self.assertLess(t.ret_pct, 30)  # the trigger saw a >30% gain, the fill got far less


class Filters(unittest.TestCase):
    def test_bad_verdict_is_not_bought(self):
        r = replay(winner(Sim(), 1000, scan="REJECT"), S)
        self.assertEqual((r.closed, r.skipped["verdict:REJECT"]), ([], 1))

    def test_unknown_and_error_verdicts_are_not_bought_either(self):
        for v in ("UNKNOWN", "ERROR", "SKIPPED"):
            self.assertEqual(replay(winner(Sim(), 1000, scan=v), S).closed, [], v)

    def test_unscanned_token_is_not_bought_when_scan_required(self):
        events = [e for e in winner(Sim(), 1000) if not hasattr(e, "verdict")]
        r = replay(events, S)
        self.assertEqual((r.closed, r.skipped["no_scan"]), ([], 1))

    def test_baseline_buys_unscanned_tokens(self):
        events = [e for e in winner(Sim(), 1000) if not hasattr(e, "verdict")]
        self.assertEqual(len(replay(events, baseline(S)).closed), 1)

    def test_big_dev_buy_is_skipped(self):
        sim = Sim()
        events = [sim.create(dev_sol=10.0), sim.scan(1100), sim.buy(1200, 1.0)]
        r = replay(events, S)
        self.assertEqual((r.closed, r.skipped["dev_buy"]), ([], 1))

    def test_late_entry_above_mcap_limit_is_skipped(self):
        sim = Sim()
        events = [sim.create(), sim.buy(1050, 40.0), sim.scan(1100), sim.buy(1200, 1.0)]
        self.assertEqual(replay(events, S).skipped["mcap_too_high"], 1)

    def test_max_open_caps_concurrent_positions(self):
        events = []
        for i in range(3):
            sim = Sim(mint=f"M{i}", t=1000)
            events += [sim.create(), sim.scan(1100 + i), sim.buy(1200 + i, 0.2)]
        r = replay(events, replace(S, max_open=1))
        self.assertEqual((len(r.closed), r.skipped["max_open"]), (1, 2))

    def test_not_enough_cash_skips_the_trade(self):
        sim = Sim()
        r = replay([sim.create(), sim.scan(1100), sim.buy(1200, 0.2)], S, start_sol=0.05)
        self.assertEqual((r.closed, r.skipped["no_cash"]), ([], 1))


class Sizing(unittest.TestCase):
    def test_fractional_sizing_compounds_with_equity(self):
        sim = Sim()
        r = replay(winner(sim, 1000), replace(S, size_frac=0.5), start_sol=2.0)
        self.assertAlmostEqual(r.closed[0].sol_in, 1.0)


class Stats(unittest.TestCase):
    def test_summary_of_a_win_and_a_loss(self):
        r = replay(winner(Sim("A"), 1000) + loser(Sim("B"), 5000), S)
        st = summarise(r)
        self.assertEqual((st.trades, st.win_rate), (2, 50.0))
        self.assertGreater(st.avg_win_pct, 0)
        self.assertLess(st.avg_loss_pct, 0)
        self.assertEqual(st.exits, {"take_profit": 1, "stop_loss": 1})
        self.assertIsNone(st.expectancy_ci95)  # 2 trades say nothing

    def test_bootstrap_interval_tracks_the_data(self):
        lo, hi = bootstrap_ci([5.0, -3.0] * 20)
        self.assertLess(lo, 1.0 < hi)  # true mean is +1; interval should straddle it
        lo, hi = bootstrap_ci([4.0] * 25)
        self.assertEqual((lo, hi), (4.0, 4.0))

    def test_drawdown(self):
        self.assertAlmostEqual(max_drawdown_pct([1.0, 1.2, 0.9, 1.1]), 25.0)
        self.assertEqual(max_drawdown_pct([1.0, 1.1, 1.2]), 0.0)


class Cli(unittest.TestCase):
    def events_file(self, n=8):
        events = []
        for i in range(n):
            sim = Sim(mint=f"MINT{i}", creator=f"C{i}", t=10_000 * (i + 1))
            events += (winner if i % 2 == 0 else loser)(sim, 10_000 * (i + 1))
        d = tempfile.mkdtemp()
        path = os.path.join(d, "e.jsonl")
        write_jsonl(path, events)
        return path, events

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = paper_main(list(argv))
        return code, out.getvalue()

    def test_replay_prints_filtered_and_baseline(self):
        path, _ = self.events_file()
        code, out = self.run_cli("replay", path, "--latency-ms", "0", "--tx-cost", "0")
        self.assertEqual(code, 0)
        self.assertIn("FILTERED", out)
        self.assertIn("BASELINE", out)
        self.assertIn("front-running", out)

    def test_sweep_reports_train_and_test(self):
        path, _ = self.events_file()
        code, out = self.run_cli("sweep", path, "--latency-ms", "0")
        self.assertEqual(code, 0)
        self.assertIn("TRAIN", out)
        self.assertIn("never seen", out)

    def test_split_keeps_each_token_on_one_side(self):
        _, events = self.events_file()
        train, test = split_events(events, 0.5)
        self.assertFalse({e.mint for e in train} & {e.mint for e in test})
        self.assertEqual(len(train) + len(test), len(events))
        self.assertTrue(max(e.t for e in train if isinstance(e, NewToken)) < min(e.t for e in test if isinstance(e, NewToken)))

    def test_missing_or_empty_file_is_a_clean_error(self):
        code, _ = self.run_cli_expect_error("replay", "/nonexistent.jsonl")
        self.assertEqual(code, 2)

    def run_cli_expect_error(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.run_cli(*argv)


if __name__ == "__main__":
    unittest.main()
