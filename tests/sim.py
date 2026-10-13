"""Builds internally consistent pump.fun event streams (reserves follow the real curve maths)."""

from sniper.pump.curve import CurveState, quote_buy
from sniper.pump.events import NewToken, ScanNote, Trade

FEE_BPS = 100


class Sim:
    def __init__(self, mint="MINT1", creator="CREATOR", t=1_000):
        self.mint, self.creator, self.t = mint, creator, t
        self.state = CurveState(30.0, 1_073_000_000.0)

    def create(self, dev_sol: float = 0.0) -> NewToken:
        tokens = self._buy(dev_sol) if dev_sol else 0.0
        return NewToken(self.t, self.mint, self.creator, "name", "SYM", tokens, dev_sol, self.state.v_sol, self.state.v_tokens)

    def scan(self, t: int, verdict: str = "PASS") -> ScanNote:
        return ScanNote(t, self.mint, verdict, 0, "")

    def buy(self, t: int, sol: float, trader: str = "BUYER") -> Trade:
        tokens = self._buy(sol)
        return Trade(t, self.mint, trader, "buy", sol, tokens, self.state.v_sol, self.state.v_tokens)

    def sell(self, t: int, tokens: float, trader: str) -> Trade:
        s = self.state
        gross = s.v_sol - s.v_sol * s.v_tokens / (s.v_tokens + tokens)
        self.state = CurveState(s.v_sol - gross, s.v_tokens + tokens)
        return Trade(t, self.mint, trader, "sell", gross * (1 - FEE_BPS / 10_000), tokens, self.state.v_sol, self.state.v_tokens)

    def _buy(self, sol: float) -> float:
        tokens = quote_buy(self.state, sol, FEE_BPS)
        self.state = CurveState(self.state.v_sol + sol * (1 - FEE_BPS / 10_000), self.state.v_tokens - tokens)
        return tokens
