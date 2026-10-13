from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"  # instant reject: the token can steal from or trap holders
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Verdict(str, Enum):
    REJECT = "REJECT"    # at least one critical finding
    UNKNOWN = "UNKNOWN"  # required data could not be fetched; never treat as safe
    AVOID = "AVOID"
    CAUTION = "CAUTION"
    PASS = "PASS"        # no red flags found by these checks. NOT a guarantee of safety


POINTS = {
    Severity.CRITICAL: 100,
    Severity.HIGH: 40,
    Severity.MEDIUM: 20,
    Severity.LOW: 8,
    Severity.INFO: 0,
}

AVOID_SCORE = 40
CAUTION_SCORE = 15

# Without these two sources the scan says nothing useful about rug/honeypot risk.
REQUIRED_SOURCES = ("mint", "holders")


@dataclass(frozen=True)
class Finding:
    check: str
    severity: Severity
    message: str


@dataclass(frozen=True)
class Holder:
    owner: str
    amount: int
    # "bonding_curve" / "pool" holders are excluded from concentration math.
    label: str | None = None


@dataclass(frozen=True)
class RoundTrip:
    """Quoted buy-then-sell of the same token, used to detect sell taxes and missing exits."""

    in_lamports: int
    tokens_out: int | None
    sol_back: int | None
    buy_error: str | None = None
    sell_error: str | None = None


@dataclass
class ScanResult:
    mint: str
    findings: list[Finding] = field(default_factory=list)
    sources_ok: dict[str, bool] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return sum(POINTS[f.severity] for f in self.findings)

    @property
    def verdict(self) -> Verdict:
        if any(f.severity is Severity.CRITICAL for f in self.findings):
            return Verdict.REJECT
        if not all(self.sources_ok.get(s) for s in REQUIRED_SOURCES):
            return Verdict.UNKNOWN
        if self.score >= AVOID_SCORE:
            return Verdict.AVOID
        if self.score >= CAUTION_SCORE:
            return Verdict.CAUTION
        return Verdict.PASS

    def to_dict(self) -> dict:
        return {
            "mint": self.mint,
            "verdict": self.verdict.value,
            "score": self.score,
            "sources_ok": self.sources_ok,
            "findings": [
                {"check": f.check, "severity": f.severity.value, "message": f.message}
                for f in self.findings
            ],
        }
