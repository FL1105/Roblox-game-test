"""How bankroll interacts with fees: what mispricing a hedge needs at each size, with no network.

Most of the fee is a percentage of the position (it scales with size), so a bigger bankroll does not make the percentage smaller. What size
changes: Kalshi rounds each order's fee up to the whole cent, which weighs most on small orders; Polymarket has a 5-share minimum; and
depth limits how much can be bought at the quoted price. Posting one leg as a resting (maker) order pays no Polymarket fee, at the price of
risk that the other leg moves before you can complete the hedge."""

from __future__ import annotations

from .arb import POLY_MIN_SHARES, kalshi_fee, poly_fee


def breakeven_edge(poly_price: float, bankroll: float, poly_maker: bool = False) -> float | None:
    """Smallest gross edge per share (how far the two asks sum below $1) that still leaves a profit after fees, for a hedge with a
    Polymarket leg at `poly_price`, using the whole bankroll. None if the bankroll cannot buy the minimum order."""
    def profit(edge: float) -> float:
        k_price = 1.0 - poly_price - edge
        if k_price <= 0.005:
            return -1.0
        n = float(int(bankroll / (poly_price + k_price + 0.04)))  # leave room for fees
        if n < POLY_MIN_SHARES:
            return -1.0
        fee = (0.0 if poly_maker else poly_fee(poly_price, n)) + kalshi_fee(k_price, n)
        return n * edge - fee

    lo, hi = 0.0, 1.0 - poly_price - 0.02  # the Kalshi leg cannot cost less than two cents
    if hi <= 0 or profit(hi) <= 0:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if profit(mid) > 0:
            hi = mid
        else:
            lo = mid
    return hi


def fee_table(prices=(0.10, 0.30, 0.50, 0.70, 0.90), bankrolls=(10, 25, 100, 500, 2500, 10000, 100000), poly_maker: bool = False) -> str:
    head = f"{'bankroll':>10s} " + " ".join(f"{'Poly @' + format(p, '.2f'):>10s}" for p in prices)
    lines = [head]
    for b in bankrolls:
        cells = []
        for p in prices:
            e = breakeven_edge(p, b, poly_maker)
            cells.append(f"{'n/a':>10s}" if e is None else f"{e * 100:9.2f}c")
        lines.append(f"${b:>9,} " + " ".join(cells))
    return "\n".join(lines)
