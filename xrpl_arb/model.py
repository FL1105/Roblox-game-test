"""Assets, amounts as they appear in rippled JSON, and address checks."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from decimal import Decimal

DROPS_PER_XRP = Decimal(1_000_000)
ONE_DROP = Decimal("0.000001")

_B58 = "rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz"  # the XRPL base58 alphabet
_HEX40 = re.compile(r"^[0-9A-Fa-f]{40}$")
_CURRENCY = re.compile(r"^(?:[A-Za-z0-9?!@#$%^&*<>(){}\[\]|]{3}|[0-9A-Fa-f]{40})$")


def is_valid_address(addr: str) -> bool:
    """True if `addr` is a well-formed classic address (prefix and double-SHA256 checksum)."""
    n = 0
    for ch in addr:
        i = _B58.find(ch)
        if i < 0:
            return False
        n = n * 58 + i
    raw = b"\x00" * (len(addr) - len(addr.lstrip(_B58[0]))) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    if len(raw) != 25 or raw[0] != 0:
        return False
    return hashlib.sha256(hashlib.sha256(raw[:21]).digest()).digest()[:4] == raw[21:]


@dataclass(frozen=True)
class Asset:
    """XRP, or an issued currency (currency code + issuer account)."""

    currency: str
    issuer: str | None = None

    def __post_init__(self):
        if self.currency == "XRP":
            if self.issuer is not None:
                raise ValueError("XRP has no issuer")
            return
        if not _CURRENCY.match(self.currency):
            raise ValueError(f"bad currency code {self.currency!r}")
        if len(self.currency) == 40:  # ledger responses use upper-case hex; make equality independent of case
            object.__setattr__(self, "currency", self.currency.upper())
        if not self.issuer or not is_valid_address(self.issuer):
            raise ValueError(f"bad issuer address {self.issuer!r} for {self.currency}")

    @property
    def is_xrp(self) -> bool:
        return self.currency == "XRP"

    @classmethod
    def parse(cls, text: str) -> "Asset":
        """'XRP' or 'CUR+rIssuerAddress'. A name that is not exactly 3 characters (DROP, bwif, X, ...) is
        accepted as written and encoded the way the ledger stores it: ASCII as 40 hex digits, zero padded."""
        text = text.strip()
        if text == "XRP":
            return XRP
        currency, sep, issuer = text.partition("+")
        if not sep:
            raise ValueError(f"expected CUR+issuer, got {text!r}")
        if len(currency) != 3 and 1 <= len(currency) <= 20 and not _HEX40.match(currency) and currency.isascii() and currency.isprintable():
            currency = currency.encode().hex().upper().ljust(40, "0")
        return cls(currency, issuer)

    def spec(self) -> dict:
        """The {currency, issuer} object rippled requests expect."""
        return {"currency": "XRP"} if self.is_xrp else {"currency": self.currency, "issuer": self.issuer}

    def __str__(self) -> str:
        return "XRP" if self.is_xrp else f"{self.currency}+{self.issuer}"

    @property
    def label(self) -> str:
        """Readable currency code: 40-hex codes are usually ASCII padded with zero bytes."""
        if len(self.currency) == 40:
            try:
                text = bytes.fromhex(self.currency).rstrip(b"\x00").decode("ascii")
                if text and text.isprintable():
                    return text
            except ValueError:
                pass
        return self.currency

    def short(self) -> str:
        return "XRP" if self.is_xrp else f"{self.label}+{self.issuer[:5]}..{self.issuer[-3:]}"


XRP = Asset("XRP")


def amount_value(amount) -> Decimal:
    """Units of a ledger amount: XRP arrives as a string of drops, an issued currency as {value,...}."""
    if isinstance(amount, str):
        return Decimal(amount) / DROPS_PER_XRP
    return Decimal(amount["value"])


def amount_asset(amount) -> Asset:
    if isinstance(amount, str):
        return XRP
    return Asset(amount["currency"], amount["issuer"])


def floor_drop(x: Decimal) -> Decimal:
    return x.quantize(ONE_DROP, rounding="ROUND_DOWN")
