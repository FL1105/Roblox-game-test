import unittest
from dataclasses import replace

from sniper.paper.engine import PaperEngine, Strategy, replay
from sniper.pump.curve import CurveState, quote_buy, quote_sell

from .sim import Sim

T0 = 1_000_000
S = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1, max_open=50, ladder=((30.0, 50.0), (60.0, 25.0)), stop_loss_pct=20.0, max_hold_s=3600)


def stream(steps, mint="M1"):
    """create, scan, a small entry buy, then `steps`: [(dt_ms, +sol buy | -tokens sell)]"""
    x = Sim(mint, "C", T0)
    ev = [x.create(), x.scan(T0 + 100), x.buy(T0 + 200, 0.01)]
    for dt, v in steps:
        ev.append(x.buy(T0 + dt, v) if v > 0 else x.sell(T0 + dt, -v, "O"))
    return ev


def run(events, strategy, start=1.0):
    eng = PaperEngine(strategy, start)
    notes = []
    eng.on_note = lambda t, k, m, x: notes.append((k, x))
    for ev in sorted(events, key=lambda e: e.t):
        eng.on_event(ev)
    return eng, eng.finish(), notes


class Rungs(unittest.TestCase):
    def test_first_rung_sells_half_the_position_and_the_rest_stays_open(self):
        eng, res, notes = run(stream([(1_000, 5.0), (2_000, 0.01)]), S)
        partial = [n for n in notes if n[0] == "partial"]
        self.assertEqual(len(partial), 1)
        self.assertIn("50%", partial[0][1])
        (t,) = res.closed
        self.assertEqual(t.reason, "end_of_data")           # the remainder was still open when the data ended
        self.assertAlmostEqual(t.sol_in, 0.1)               # one position, measured against what was originally paid

    def test_two_rungs_in_one_jump_sell_together_in_one_transaction(self):
        eng, res, notes = run(stream([(1_000, 12.0), (2_000, 0.01)]), S)
        partial = [n for n in notes if n[0] == "partial"]
        self.assertEqual(len(partial), 1)
        self.assertIn("75%", partial[0][1])

    def test_rungs_fire_one_after_another_as_price_climbs(self):
        eng, res, notes = run(stream([(1_000, 5.0), (2_000, 0.01), (3_000, 5.0), (4_000, 0.01)]), S)
        partial = [n for n in notes if n[0] == "partial"]
        self.assertEqual([("50%" in n[1], "25%" in n[1]) for n in partial], [(True, False), (False, True)])

    def test_fractions_that_total_one_close_the_position_on_the_last_rung(self):
        s = replace(S, ladder=((30.0, 50.0), (60.0, 50.0)))
        eng, res, notes = run(stream([(1_000, 5.0), (2_000, 0.01), (3_000, 5.0), (4_000, 0.01)]), s)
        (t,) = res.closed
        self.assertEqual(t.reason, "ladder_2")
        self.assertEqual(eng.positions, {})

    def test_ladder_replaces_the_single_take_profit_in_targets_mode(self):
        s = replace(S, take_profit_pct=30.0)
        _, res, _ = run(stream([(1_000, 5.0), (2_000, 0.01)]), s)
        self.assertNotEqual(res.closed[0].reason, "take_profit")
        _, res, _ = run(stream([(1_000, 5.0), (2_000, 0.01)]), replace(S, ladder=()))
        self.assertEqual(res.closed[0].reason, "take_profit")


class Accounting(unittest.TestCase):
    def test_cash_always_equals_start_plus_total_pnl(self):
        for tx in (0.0, 0.003):
            for steps in ([(1_000, 5.0), (2_000, 0.01)], [(1_000, 12.0)], [(1_000, 5.0), (3_000, 5.0), (5_000, -2.5e8)]):
                eng, res, _ = run(stream(steps), replace(S, tx_cost_sol=tx))
                self.assertAlmostEqual(res.final_equity, 1.0 + sum(t.pnl_sol for t in res.closed), places=9, msg=(tx, steps))

    def test_every_sale_pays_its_own_transaction_cost(self):
        calm = replace(S, ladder_breakeven_stop=False, stop_loss_pct=99.0)  # same decisions at both cost levels, so only the accounting differs
        free = run(stream([(1_000, 12.0), (2_000, 0.01), (3_000, 5.0)]), calm)[1]
        paid = run(stream([(1_000, 12.0), (2_000, 0.01), (3_000, 5.0)]), replace(calm, tx_cost_sol=0.01))[1]
        # one entry + one combined partial sale + the final sale = 3 transactions
        self.assertAlmostEqual(free.closed[0].pnl_sol - paid.closed[0].pnl_sol, 0.03, places=9)

    def test_a_ladder_costs_more_in_fees_than_selling_everything_once(self):
        steps = [(1_000, 5.0), (2_000, 0.01), (3_000, 5.0), (4_000, 0.01)]
        calm = replace(S, ladder_breakeven_stop=False, stop_loss_pct=99.0, tx_cost_sol=0.003)
        _, res, notes = run(stream(steps), calm)
        sales = 1 + len([n for n in notes if n[0] == "partial"])  # the final sale plus one per rung
        entry_and_sales = 1 + sales
        self.assertEqual(entry_and_sales, 4)  # entry + two rungs + the final sale: four transactions, not two
        free = run(stream(steps), replace(calm, tx_cost_sol=0.0))[1].closed[0]
        self.assertAlmostEqual(free.pnl_sol - res.closed[0].pnl_sol, 4 * 0.003, places=9)

    def test_the_realised_proceeds_include_every_rung(self):
        eng, res, _ = run(stream([(1_000, 5.0), (2_000, 0.01)]), S)
        (t,) = res.closed
        entry_tokens = quote_buy(CurveState(30.0, 1_073_000_000.0), 0.01, 123.8)  # (only to show the position is tiny and real)
        self.assertGreater(t.proceeds, 0)
        self.assertAlmostEqual(t.pnl_sol, t.proceeds - t.sol_in, places=9)  # zero transaction costs in this strategy


class StopsAfterRungs(unittest.TestCase):
    def fall_back(self):
        # rung 1 hits, then the price falls back below the entry price
        return stream([(1_000, 5.0), (2_000, 0.01), (3_000, -2.4e8), (4_000, 0.01)])

    def test_stop_moves_to_breakeven_after_the_first_rung(self):
        _, res, _ = run(self.fall_back(), S)
        self.assertEqual(res.closed[0].reason, "breakeven_stop")

    def test_without_the_breakeven_rule_the_normal_stop_applies(self):
        _, res, _ = run(self.fall_back(), replace(S, ladder_breakeven_stop=False, stop_loss_pct=90.0))
        self.assertNotEqual(res.closed[0].reason, "breakeven_stop")

    def test_remainder_can_ride_a_trailing_stop(self):
        steps = [(1_000, 5.0), (2_000, 0.01), (3_000, 8.0), (4_000, 0.01), (5_000, -9.0e7), (6_000, 0.01)]
        _, res, _ = run(stream(steps), replace(S, trailing_stop_pct=10.0))
        self.assertEqual(res.closed[0].reason, "trailing_stop")

    def test_a_stop_before_any_rung_closes_the_whole_position_once(self):
        steps = [(1_000, 0.01), (2_000, 2.0), (3_000, -2.2e8), (4_000, 0.01)]
        _, res, notes = run(stream(steps), S)
        self.assertEqual([n for n in notes if n[0] == "partial"], [])


class WithLatency(unittest.TestCase):
    def test_partial_sale_fills_at_the_price_after_the_delay(self):
        x = Sim("M1", "C", T0)
        ev = [x.create(), x.scan(T0 + 100), x.buy(T0 + 700, 0.01), x.buy(T0 + 2_000, 5.0), x.buy(T0 + 2_200, 6.0), x.buy(T0 + 4_000, 0.01)]
        eng = PaperEngine(replace(S, latency_ms=500), 1.0)
        for e in ev:
            eng.on_event(e)
        # rung triggers on the 5 SOL buy (t=2000); the sale is at t=2500 against the state after the 6 SOL buy at t=2200
        pos = eng.positions["M1"]
        self.assertGreaterEqual(pos.rungs_done, 1)
        self.assertGreater(pos.realized_proceeds, 0.0)


class WithSmartMode(unittest.TestCase):
    def test_ladder_and_smart_exit_coexist(self):
        eng, res, notes = run(stream([(1_000, 5.0), (2_000, 0.01), (20_000, -3.0e7), (21_000, 0.01)]), replace(S, exit_mode="smart"))
        self.assertTrue(any(n[0] == "partial" for n in notes))
        self.assertTrue(res.closed)


if __name__ == "__main__":
    unittest.main()
