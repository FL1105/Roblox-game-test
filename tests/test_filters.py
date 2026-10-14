import unittest
from dataclasses import replace

from sniper.paper.engine import Strategy, replay

from .sim import Sim

S = Strategy(latency_ms=0, tx_cost_sol=0.0, size_sol=0.1, max_open=50)
T0 = 1_000_000
DAY = 86_400_000


def token(mint="M1", creator="C", t0=T0, moves=(), scan_at=100):
    x = Sim(mint, creator, t0)
    ev = [x.create()]
    for dt, sol in moves:
        ev.append(x.buy(t0 + dt, sol) if sol > 0 else x.sell(t0 + dt, -sol, "O"))
    ev.append(x.scan(t0 + scan_at))
    ev.append(x.buy(t0 + scan_at + 5_000, 0.01))  # something to fill the entry against
    return ev


def trades(events, strategy):
    r = replay(events, strategy)
    return len(r.closed), r.skipped


class TradeCount(unittest.TestCase):
    def test_untouched_token_is_skipped_but_one_with_activity_is_not(self):
        s = replace(S, min_trades=2)
        self.assertEqual(trades(token(), s)[1]["too_few_trades"], 1)
        n, skipped = trades(token(moves=[(10, 0.2), (20, 0.2)]), s)
        self.assertEqual((n, skipped["too_few_trades"]), (1, 0))

    def test_off_by_default(self):
        self.assertEqual(trades(token(), S)[0], 1)


class TokenAge(unittest.TestCase):
    def test_late_decisions_are_skipped(self):
        s = replace(S, max_token_age_s=10)
        self.assertEqual(trades(token(scan_at=30_000), s)[1]["too_old"], 1)
        self.assertEqual(trades(token(scan_at=5_000), s)[0], 1)


class AntiChasing(unittest.TestCase):
    def test_token_already_up_a_lot_is_not_chased(self):
        pumped = token(moves=[(10, 8.0)])  # ~+60% before the scan clears
        self.assertEqual(trades(pumped, replace(S, max_entry_gain_pct=20))[1]["chasing"], 1)
        self.assertEqual(trades(pumped, replace(S, max_entry_gain_pct=200))[0], 1)


class CreatorHistory(unittest.TestCase):
    def serial(self, n, gap=60_000, creator="SERIAL"):
        ev = []
        for i in range(n):
            ev += token(f"S{i}", creator, T0 + i * gap)
        return ev

    def test_serial_launchers_are_skipped_after_the_allowed_number(self):
        r = replay(self.serial(5), replace(S, max_creator_launches=2))
        self.assertEqual(r.skipped["serial_creator"], 2)       # launches 4 and 5 each follow 3+ earlier ones; launch 3 follows 2
        self.assertEqual(len(r.closed), 3)

    def test_zero_means_only_first_time_creators(self):
        r = replay(self.serial(3), replace(S, max_creator_launches=0))
        self.assertEqual((len(r.closed), r.skipped["serial_creator"]), (1, 2))

    def test_launches_older_than_a_day_do_not_count(self):
        ev = token("OLD", "C", T0) + token("NEW", "C", T0 + 2 * DAY)
        self.assertEqual(replay(ev, replace(S, max_creator_launches=0)).skipped["serial_creator"], 0)

    def test_other_creators_are_unaffected(self):
        ev = self.serial(3, creator="A") + token("B0", "B", T0 + 500_000)
        r = replay(ev, replace(S, max_creator_launches=0))
        self.assertEqual(r.skipped["serial_creator"], 2)
        self.assertEqual(len(r.closed), 2)  # A's first launch and B's first launch

    def collapsed(self, prior_moves):
        x = Sim("P1", "CC", T0)
        prior = [x.create(), x.scan(T0 + 100)]
        for dt, sol in prior_moves:
            prior.append(x.buy(T0 + dt, sol) if sol > 0 else x.sell(T0 + dt, -sol, "O"))
        return prior + token("NEXT", "CC", T0 + 200_000)

    def test_a_creator_whose_token_gave_back_its_gain_is_blocked(self):
        ev = self.collapsed([(10, 8.0), (30_000, -2.2e8)])   # up ~60%, then nearly all of it given back
        s = replace(S, block_collapsed_creators=True)
        self.assertEqual(replay(ev, s).skipped["collapsed_creator"], 1)
        self.assertEqual(replay(ev, S).skipped["collapsed_creator"], 0)

    def test_a_creator_whose_token_kept_its_gain_is_not_blocked(self):
        ev = self.collapsed([(10, 8.0)])
        self.assertEqual(replay(ev, replace(S, block_collapsed_creators=True)).skipped["collapsed_creator"], 0)

    def test_a_creator_whose_token_never_moved_is_not_blocked(self):
        ev = self.collapsed([])
        self.assertEqual(replay(ev, replace(S, block_collapsed_creators=True)).skipped["collapsed_creator"], 0)


class WithConfirmMode(unittest.TestCase):
    def test_filters_apply_when_a_watched_token_finally_confirms(self):
        pumped = token(moves=[(10, 2.0), (20, 6.0)], scan_at=100)
        s = replace(S, entry_mode="confirm", confirm_gain_pct=10, max_entry_gain_pct=30)
        r = replay(pumped, s)
        self.assertEqual((len(r.closed), r.skipped["chasing"]), (0, 1))  # confirmed, but by then it was too far up


if __name__ == "__main__":
    unittest.main()
