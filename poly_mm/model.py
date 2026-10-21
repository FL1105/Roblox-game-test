"""Paper market maker over a recorded tape.

The tape lists every taker trade. A taker BUY of one outcome at price p is the same as a taker SELL of the other outcome at 1 - p (the
exchange matches them by minting a pair), so each trade is "pressure" on one outcome's bids at one price. A resting bid of ours at
price b is filled by pressure at a price below b (we would have been the best bid, so the seller reaches us first) for as many shares as
that trade sold, and by `queue` of the shares sold at exactly b (we shared the level with others). We fill at our own price, pay no fee (makers are not charged), and take no rebate.

What this cannot know: how deep the queue ahead of us was, how our quotes would change other people's behaviour, and the latency to
the exchange. The `queue` setting spans the optimistic and pessimistic ends; read results across it, not at one point."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .btc import up_probability
from .markets import Market
from .tape import Trade

TICK = 0.01


@dataclass(frozen=True)
class Quoter:
    delta: float = 0.02  # how far below fair each bid sits (0.02 around 0.50 gives the 0.48 / 0.48 of the tweet)
    size: float = 50.0  # shares per bid
    refresh_s: float = 10.0  # how often quotes are recomputed from the latest price
    latency_s: float = 1.0  # delay before a refreshed quote is live
    stop_before_s: float = 20.0  # no quoting in the last seconds of the window
    start_after_s: float = 0.0  # no quoting in the first seconds
    skew: float = 0.0  # price units each share of imbalance moves the bids (0.0002 = 50 shares of imbalance moves them one cent)
    max_imbalance: float = 200.0  # stop bidding the heavier side beyond this many shares more than the other
    fair: str = "last"  # "last" trade-implied Up price, "btc" the price implied by Coinbase BTC, or "static" 0.50 (the tweet's version)
    queue: float = 0.3  # share of pressure at exactly our price that fills us


@dataclass
class Result:
    slug: str
    up: float = 0.0  # shares of Up held at the end
    down: float = 0.0
    cost: float = 0.0
    fills: int = 0
    pnl: float = 0.0
    winner: int = 0
    volume: float = 0.0  # dollars filled


def pressure(trades: list[Trade]) -> list[tuple[int, int, float, float]]:
    """(time, outcome whose bids are hit, price they are hit at, shares)."""
    out = []
    for t in trades:
        if t.side == "SELL":
            out.append((t.t, t.outcome, t.price, t.size))
        else:  # a BUY of this outcome at p lifts bids on the other outcome at 1 - p
            out.append((t.t, 1 - t.outcome, 1.0 - t.price, t.size))
    return out


def floor_tick(x: float) -> float:
    return math.floor(x / TICK + 1e-9) * TICK


def simulate(m: Market, trades: list[Trade], q: Quoter, btc=None) -> Result:
    res = Result(m.slug, winner=m.winner)
    events = pressure([t for t in trades if m.start - 1 <= t.t <= m.end])
    up_prices = [(t.t, t.price if t.outcome == 0 else 1 - t.price) for t in trades if t.t <= m.end]
    live_until = m.end - q.stop_before_s
    j, last_up = 0, 0.5
    next_ref = m.start + q.start_after_s  # time of the next quote computation
    bid = [0.0, 0.0]  # live bids (Up, Down); 0 = none
    left = [0.0, 0.0]  # shares still on offer at those bids
    for t, side, x, sz in events:
        while next_ref < live_until and next_ref + q.latency_s <= t:  # quotes computed before this event and live by now
            while j < len(up_prices) and up_prices[j][0] <= next_ref:
                last_up = up_prices[j][1]
                j += 1
            fair = last_up if q.fair == "last" else 0.5
            if q.fair == "btc" and btc is not None:
                spot, strike = btc.price(next_ref), btc.price(m.start)
                if spot and strike:
                    fair = up_probability(spot, strike, m.end - next_ref, btc.vol_per_min(next_ref))
            lean = q.skew * (res.up - res.down)  # long Up: bid less for more Up, more for Down
            bu, bd = floor_tick(fair - q.delta - lean), floor_tick(1 - fair - q.delta + lean)
            bid = [bu if 0.01 <= bu <= 0.98 else 0.0, bd if 0.01 <= bd <= 0.98 else 0.0]
            left = [q.size, q.size]
            next_ref += q.refresh_s
        if t >= live_until or bid[side] <= 0:
            continue
        if side == 0 and res.up - res.down >= q.max_imbalance or side == 1 and res.down - res.up >= q.max_imbalance:
            continue
        b = bid[side]
        if x < b - 1e-9:
            take = min(left[side], sz)  # we are best bid, so the seller reaches us first; but only `sz` shares were sold
        elif abs(x - b) <= 1e-9:
            take = min(left[side], sz * q.queue)  # shares sold at our level, shared with whoever was already there
        else:
            continue
        if take <= 0:
            continue
        left[side] -= take
        res.fills += 1
        res.cost += take * b
        res.volume += take * b
        if side == 0:
            res.up += take
        else:
            res.down += take
    payout = res.up if m.winner == 0 else res.down
    res.pnl = payout - res.cost
    return res


@dataclass
class Summary:
    markets: int
    pnl: float
    per_market: float
    volume: float
    margin_on_volume: float
    win_rate: float
    fills: int
    pair_cost: float | None  # average Up price + average Down price over markets where both filled
    worst: float
    imbalance: float  # average |Up - Down| / (Up + Down)
    sharpe_like: float


def summarize(results: list[Result]) -> Summary:
    n = len(results)
    pnl = sum(r.pnl for r in results)
    vol = sum(r.volume for r in results)
    pcs = [r.cost / max(r.up + r.down, 1e-9) * 2 for r in results if r.up > 0 and r.down > 0]
    imb = [abs(r.up - r.down) / (r.up + r.down) for r in results if r.up + r.down > 0]
    pm = [r.pnl for r in results]
    mean = pnl / n if n else 0.0
    sd = (sum((x - mean) ** 2 for x in pm) / n) ** 0.5 if n else 0.0
    return Summary(n, pnl, mean, vol, pnl / vol if vol else 0.0, sum(1 for r in results if r.pnl > 0) / n if n else 0.0, sum(r.fills for r in results),
                   sum(pcs) / len(pcs) if pcs else None, min(pm) if pm else 0.0, sum(imb) / len(imb) if imb else 0.0, mean / sd * n ** 0.5 if sd else 0.0)
