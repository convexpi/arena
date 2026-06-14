"""Tests for the risk engine: drawdown tracking, position limits, survival score."""

import pytest
from convexpi.arena.risk import RiskEngine
from convexpi.arena.market import Account


def make_accounts(agents: dict[str, tuple[int, int]]) -> dict[str, Account]:
    """agents: {agent_id: (cash_cents, position)}"""
    accounts = {}
    for aid, (cash, pos) in agents.items():
        acc = Account(aid)
        acc.cash = cash
        acc.position = pos
        accounts[aid] = acc
    return accounts


INITIAL_CASH = 100_000   # $1000 in cents
MARK = 10_000            # $100 per share in cents


class TestRiskEngineDrawdown:
    def test_no_elimination_within_limit(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        # Agent is flat, no PnL change
        accounts = make_accounts({"alice": (INITIAL_CASH, 0)})
        eliminated = risk.check(accounts, MARK, tick=1)
        assert eliminated == []
        assert not risk.is_eliminated("alice")

    def test_eliminated_on_drawdown_breach(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        # Peak = $1000. Loss of $600 (> $500 limit).
        accounts = make_accounts({"alice": (INITIAL_CASH - 60_000, 0)})  # -$600
        eliminated = risk.check(accounts, MARK, tick=5)
        assert "alice" in eliminated
        assert risk.is_eliminated("alice")

    def test_not_eliminated_at_exact_limit(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        # Exactly at limit (not exceeded)
        accounts = make_accounts({"alice": (INITIAL_CASH - 50_000, 0)})  # -$500
        eliminated = risk.check(accounts, MARK, tick=1)
        assert eliminated == []

    def test_peak_value_tracked(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        # First tick: gain $200
        accounts = make_accounts({"alice": (INITIAL_CASH + 20_000, 0)})
        risk.check(accounts, MARK, tick=1)
        # Second tick: lose $300 from peak (not from initial)
        accounts = make_accounts({"alice": (INITIAL_CASH + 20_000 - 30_000, 0)})
        eliminated = risk.check(accounts, MARK, tick=2)
        assert eliminated == []  # peak was $1200; dd is $300 < $500

    def test_already_eliminated_not_re_eliminated(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        accounts = make_accounts({"alice": (INITIAL_CASH - 60_000, 0)})
        first = risk.check(accounts, MARK, tick=1)
        second = risk.check(accounts, MARK, tick=2)
        assert "alice" in first
        assert "alice" not in second   # not returned again

    def test_drawdown_with_open_position(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        # Agent holds 5 shares bought at $100; mark is now $80 → mark-to-market loss
        # cash = 100_000 - 5*10_000 = 50_000; position = 5; mark = 8000 ($80)
        accounts = make_accounts({"alice": (50_000, 5)})
        mark = 8_000   # $80 per share — $100 loss on 5 shares = -$100 total
        eliminated = risk.check(accounts, mark, tick=1)
        assert eliminated == []   # only $100 loss, under $500 limit


class TestRiskEnginePositionLimit:
    def test_eliminated_on_long_breach(self):
        risk = RiskEngine(
            max_drawdown_dollars=999_999.0,
            position_limit=100,
            initial_cash_dollars=1000.0
        )
        accounts = make_accounts({"alice": (INITIAL_CASH, 101)})
        eliminated = risk.check(accounts, MARK, tick=1)
        assert "alice" in eliminated

    def test_eliminated_on_short_breach(self):
        risk = RiskEngine(
            max_drawdown_dollars=999_999.0,
            position_limit=100,
            initial_cash_dollars=1000.0
        )
        accounts = make_accounts({"alice": (INITIAL_CASH, -101)})
        eliminated = risk.check(accounts, MARK, tick=1)
        assert "alice" in eliminated

    def test_not_eliminated_at_limit(self):
        risk = RiskEngine(
            max_drawdown_dollars=999_999.0,
            position_limit=100,
            initial_cash_dollars=1000.0
        )
        accounts = make_accounts({"alice": (INITIAL_CASH, 100)})
        eliminated = risk.check(accounts, MARK, tick=1)
        assert eliminated == []

    def test_no_position_limit_by_default(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        accounts = make_accounts({"alice": (INITIAL_CASH, 9999)})
        eliminated = risk.check(accounts, MARK, tick=1)
        assert eliminated == []


class TestRiskEngineSurvivalScore:
    def test_flat_agent_score(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        accounts = make_accounts({"alice": (INITIAL_CASH, 0)})
        risk.check(accounts, MARK, tick=1)
        rows = risk.score(accounts, MARK)
        assert len(rows) == 1
        # No drawdown, no PnL → survival score should be 0 or defined
        assert rows[0]["agent_id"] == "alice"

    def test_profitable_agent_positive_score(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        # First gain (sets peak)
        accounts_up = make_accounts({"alice": (INITIAL_CASH + 20_000, 0)})
        risk.check(accounts_up, MARK, tick=1)
        # Then small dip (creates max_drawdown)
        accounts_dip = make_accounts({"alice": (INITIAL_CASH + 10_000, 0)})
        risk.check(accounts_dip, MARK, tick=2)
        rows = risk.score(accounts_dip, MARK)
        assert rows[0]["survival_score"] > 0

    def test_eliminated_agent_sorted_last(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        accounts = make_accounts({
            "alice": (INITIAL_CASH - 60_000, 0),   # eliminated
            "bob":   (INITIAL_CASH + 10_000, 0),   # healthy
        })
        risk.check(accounts, MARK, tick=1)
        rows = risk.score(accounts, MARK)
        assert rows[-1]["agent_id"] == "alice"

    def test_multiple_agents_independent(self):
        risk = RiskEngine(max_drawdown_dollars=500.0, initial_cash_dollars=1000.0)
        accounts = make_accounts({
            "alice": (INITIAL_CASH - 60_000, 0),   # eliminated
            "bob":   (INITIAL_CASH, 0),             # healthy
        })
        eliminated = risk.check(accounts, MARK, tick=1)
        assert "alice" in eliminated
        assert "bob" not in eliminated
