"""XRPL assets and amounts. Model arithmetic is float; `fmt_iou` / `to_drops` produce the exact strings a transaction needs."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

DROPS = 1_000_000


@dataclass(frozen=True)
class Asset:
    currency: str  # "XRP", a 3-character code, or 40 hex characters
    issuer: str | None = None

    @property
    def is_xrp(self) -> bool:
        return self.issuer is None

    @property
    def key(self) -> str:
        return "XRP" if self.is_xrp else f"{self.currency}+{self.issuer}"

    @property
    def code(self) -> str:
        """Readable currency code (40-hex codes are decoded when they are printable ASCII)."""
        c = self.currency
        if len(c) == 40:
            try:
                s = bytes.fromhex(c).rstrip(b"\0").decode("ascii")
            except ValueError:
                return c[:8]
            return s if s and s.isprintable() else c[:8]
        return c

    def to_json(self) -> dict:
        return {"currency": "XRP"} if self.is_xrp else {"currency": self.currency, "issuer": self.issuer}

    @classmethod
    def parse(cls, text: str) -> "Asset":
        """'XRP' or 'CURRENCY+rIssuer'."""
        text = text.strip()
        if text.upper() == "XRP":
            return cls("XRP")
        cur, sep, issuer = text.partition("+")
        if not sep or not cur or not issuer.startswith("r"):
            raise ValueError(f"asset must be 'XRP' or 'CURRENCY+rIssuer', got {text!r}")
        return cls(cur, issuer)

    @classmethod
    def from_amount(cls, amount) -> "Asset":
        if isinstance(amount, str):
            return cls("XRP")
        return cls(amount["currency"], amount["issuer"])


def amount_value(amount) -> float:
    """Value of an XRPL Amount in whole units (XRP drops strings become XRP)."""
    return int(amount) / DROPS if isinstance(amount, str) else float(amount["value"])


def to_drops(xrp: float, rounding=ROUND_DOWN) -> str:
    return str(int((Decimal(repr(xrp)) * DROPS).to_integral_value(rounding)))


def fmt_iou(value: float, digits: int = 15) -> str:
    """Issued-currency value with at most `digits` significant digits, rounded down so we never promise more than we hold."""
    d = Decimal(repr(value))
    if d == 0:
        return "0"
    exp = d.adjusted() - (digits - 1)
    q = d.quantize(Decimal(1).scaleb(exp), rounding=ROUND_DOWN)
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def amount_json(asset: Asset, value: float, rounding=ROUND_DOWN):
    """An XRPL Amount for `value` units of `asset`."""
    if asset.is_xrp:
        return to_drops(value, rounding)
    return {"currency": asset.currency, "issuer": asset.issuer, "value": fmt_iou(value)}
