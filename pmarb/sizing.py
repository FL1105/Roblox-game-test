"""How order size and bankroll change the economics: fee rounding, depth, and capital per venue."""

from __future__ import annotations

import math

from .model import Fee, Opportunity, build

KALSHI = Fee(0.07, 1.0, cent_round=True)


def fee_per_contract(price: float, qty: int, fee: Fee = KALSHI) -> float:
    """Taker fee per contract in dollars for an order of `qty` at `price`."""
    return fee.total(price, qty) / qty


def overhead(price: float, qty: int, fee: Fee = KALSHI) -> float:
    """How much more the rounded fee costs than the unrounded formula (0.2 = 20% more)."""
    raw = fee.rate * price * (1.0 - price)
    return fee_per_contract(price, qty, fee) / raw - 1.0 if raw else 0.0


def net_at(opp: Opportunity, qty: int) -> float | None:
    """The same hedge re-priced at a smaller order (fees re-rounded). None if the order is not allowed or not available."""
    if qty < 1 or qty > opp.qty or any(qty < leg.min_size for leg in opp.legs):
        return None
    cost = qty * sum(leg.price for leg in opp.legs)
    fees = sum(leg.fee.total(leg.price, qty) for leg in opp.legs)
    return qty * opp.payout_min - cost - fees


def capital_cap(prices_by_venue: dict[str, float], bankroll: float, split: dict[str, float] | None = None) -> int:
    """Sets you can afford when the bankroll is split between venues (evenly by default); each leg is paid for on its own venue."""
    venues = list(prices_by_venue)
    split = split or {v: 1.0 / len(venues) for v in venues}
    return int(min(bankroll * split[v] / prices_by_venue[v] for v in venues))


def cost_to_buy(levels: list[tuple[float, float]], qty: int) -> float | None:
    """Total cost of buying `qty` contracts walking an ask ladder [(price, size)] best first; None if the book is too thin."""
    left, cost = qty, 0.0
    for price, size in levels:
        take = min(left, size)
        cost += take * price
        left -= take
        if left <= 0:
            return cost
    return None


def hedge_net(asks_a: list[tuple[float, float]], asks_b: list[tuple[float, float]], fee_a: Fee, fee_b: Fee, qty: int,
              payout: float = 1.0) -> float | None:
    """Guaranteed profit of buying `qty` of leg A and `qty` of leg B (each walked down its own ask ladder), after fees.

    Fees use the average fill price of each leg. None when either book cannot supply `qty`.
    """
    ca, cb = cost_to_buy(asks_a, qty), cost_to_buy(asks_b, qty)
    if ca is None or cb is None:
        return None
    return qty * payout - ca - cb - fee_a.total(ca / qty, qty) - fee_b.total(cb / qty, qty)


def best_size(asks_a, asks_b, fee_a: Fee, fee_b: Fee, grid=(1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000)) -> list[tuple[int, float]]:
    """(qty, net) along a size grid, only where the hedge can be filled."""
    return [(q, n) for q in grid if (n := hedge_net(asks_a, asks_b, fee_a, fee_b, q)) is not None]
