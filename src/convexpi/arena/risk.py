"""
risk.py — Survival-scoring risk engine for the Arena.

Tracks each agent's mark-to-market value against their personal high-water mark
and eliminates those that breach the maximum drawdown limit. Eliminated agents
have their positions force-liquidated at the start of the next tick.

Pedagogical design:
  - High-water mark rule: drawdown is measured from each agent's personal peak,
    not from a fixed starting value. This is how real hedge fund risk limits work.
  - Liquidation is a market order submitted the next tick and may execute at a
    poor price — a deliberate lesson in why drawdown limits should be set
    conservatively, not as a last-resort backstop.
  - Survival score = PnL / max_drawdown, the simplest risk-adjusted metric.
    A strategy that makes $500 with $100 max drawdown beats one that makes $800
    with $600 max drawdown. Eliminated agents score below all survivors.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class AgentRisk:
    """Risk state for one agent (all monetary values in dollars)."""
    peak_value: float          # high-water mark
    max_drawdown: float = 0.0  # largest drawdown seen
    eliminated_tick: Optional[int] = None
    elimination_reason: str = ""

    @property
    def eliminated(self) -> bool:
        return self.eliminated_tick is not None


class RiskEngine:
    """
    Per-agent drawdown enforcement and survival scoring.

    Parameters
    ----------
    max_drawdown_dollars : float
        An agent is eliminated when their mark-to-market value falls more than
        this amount (in dollars) below their personal peak. E.g., 500.0 means
        a $500 drop from peak triggers force-liquidation.
    position_limit : int | None
        Maximum absolute share position. Breaching this also triggers
        elimination. None disables position limits.
    initial_cash_dollars : float
        Starting account value, used to seed each agent's peak and compute
        percentage-based metrics in the score output.
    """

    def __init__(
        self,
        max_drawdown_dollars: float = 500.0,
        position_limit: Optional[int] = None,
        initial_cash_dollars: float = 1000.0,
    ):
        self.max_drawdown_dollars = max_drawdown_dollars
        self.position_limit = position_limit
        self.initial_cash_dollars = initial_cash_dollars
        self._state: dict[str, AgentRisk] = {}

    def _get(self, agent_id: str) -> AgentRisk:
        if agent_id not in self._state:
            self._state[agent_id] = AgentRisk(peak_value=self.initial_cash_dollars)
        return self._state[agent_id]

    def check(self, accounts: dict, mark_cents: float, tick: int) -> list[str]:
        """
        Evaluate all accounts against risk limits for this tick.

        Returns the list of agent_ids *newly* eliminated (agents that were
        already eliminated are skipped). `mark_cents` is the last trade or
        fundamental price in integer cents.
        """
        newly_eliminated = []
        for aid, acct in accounts.items():
            if aid == "__seed__":
                continue
            rs = self._get(aid)
            if rs.eliminated:
                continue

            value = acct.value(mark_cents) / 100  # dollars
            rs.peak_value = max(rs.peak_value, value)
            drawdown = rs.peak_value - value
            rs.max_drawdown = max(rs.max_drawdown, drawdown)

            reason = None
            if drawdown > self.max_drawdown_dollars:
                reason = (f"drawdown ${drawdown:.0f} > limit "
                          f"${self.max_drawdown_dollars:.0f}")
            elif self.position_limit and abs(acct.position) > self.position_limit:
                reason = (f"position {acct.position:+d} > limit "
                          f"±{self.position_limit}")

            if reason:
                rs.eliminated_tick = tick
                rs.elimination_reason = reason
                newly_eliminated.append(aid)

        return newly_eliminated

    def score(self, accounts: dict, mark_cents: float) -> list[dict]:
        """
        Return a survival-scored leaderboard, best to worst.

        survival_score = PnL / max(max_drawdown, $1)
        Eliminated agents are sorted last regardless of PnL.
        """
        rows = []
        for aid, acct in accounts.items():
            if aid == "__seed__":
                continue
            rs = self._get(aid)
            pnl = acct.value(mark_cents) / 100 - self.initial_cash_dollars
            max_dd = max(rs.max_drawdown, 0.01)
            # Eliminated agents get a massive penalty so they sort last
            survival_score = pnl / max_dd if not rs.eliminated else pnl / max_dd - 1e6
            rows.append({
                "agent_id": aid,
                "pnl": round(pnl, 2),
                "position": acct.position,
                "peak_value": round(rs.peak_value, 2),
                "max_drawdown": round(rs.max_drawdown, 2),
                "survival_score": round(survival_score, 3),
                "eliminated": rs.eliminated,
                "eliminated_tick": rs.eliminated_tick,
                "elimination_reason": rs.elimination_reason,
            })
        rows.sort(key=lambda r: (-int(not r["eliminated"]), -r["survival_score"]))
        return rows

    def is_eliminated(self, agent_id: str) -> bool:
        return self._state.get(agent_id, AgentRisk(peak_value=0)).eliminated
