"""Tests for Market simulation loop, accounting, and leaderboard.

Market accounting:
  - Agent accounts start with cash=0, position=0
  - Fills update position and cash (no initial allocation)
  - The seed book provides initial liquidity around the fundamental value (~10,000 cents)
  - Market buy orders fill against seed asks; market sell orders against seed bids
"""

import pytest
from convexpi.arena.market import Market, Account
from convexpi.arena.agents import Agent, MarketState
from convexpi.arena.engine import Side, Order, OrderType


class PassiveAgent(Agent):
    """Does nothing."""
    def on_tick(self, state):
        return []


class MarketBuyAgent(Agent):
    """Submits one market buy on the first tick."""
    def __init__(self, agent_id, qty):
        super().__init__(agent_id, seed=0)
        self.qty = qty
        self.submitted = False

    def on_tick(self, state):
        if not self.submitted:
            self.submitted = True
            return [self.market(Side.BUY, self.qty)]
        return []


class MarketSellAgent(Agent):
    """Submits one market sell on the first tick."""
    def __init__(self, agent_id, qty):
        super().__init__(agent_id, seed=0)
        self.qty = qty
        self.submitted = False

    def on_tick(self, state):
        if not self.submitted:
            self.submitted = True
            return [self.market(Side.SELL, self.qty)]
        return []


class FillTracker(Agent):
    """Records on_fill callbacks."""
    def __init__(self, agent_id, side, qty):
        super().__init__(agent_id, seed=0)
        self.side = side
        self.qty = qty
        self.fills = []
        self.submitted = False

    def on_tick(self, state):
        if not self.submitted:
            self.submitted = True
            if self.side == Side.BUY:
                return [self.market(Side.BUY, self.qty)]
            else:
                return [self.market(Side.SELL, self.qty)]
        return []

    def on_fill(self, trade, side, qty, price):
        self.fills.append((side, qty, price))


class TestMarketAccounting:
    def _run(self, agents, n_ticks=5):
        m = Market(agents, n_ticks=n_ticks, seed=1)
        m.run(verbose_every=0)
        return m

    def test_accounts_created_for_all_agents(self):
        agents = [PassiveAgent("a", seed=0), PassiveAgent("b", seed=1)]
        m = Market(agents, n_ticks=2, seed=1)
        assert "a" in m.accounts
        assert "b" in m.accounts

    def test_no_trades_cash_zero(self):
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=3, seed=1)
        m.run(verbose_every=0)
        assert m.accounts["a"].cash == 0
        assert m.accounts["a"].position == 0

    def test_market_buy_updates_position(self):
        buyer = MarketBuyAgent("buyer", qty=5)
        m = Market([buyer], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        assert m.accounts["buyer"].position == 5

    def test_market_buy_deducts_cash(self):
        buyer = MarketBuyAgent("buyer", qty=5)
        m = Market([buyer], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        # Bought shares → cash should be negative (no initial cash)
        assert m.accounts["buyer"].cash < 0

    def test_market_sell_adds_cash(self):
        seller = MarketSellAgent("seller", qty=5)
        m = Market([seller], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        # Sold shares → cash positive, position negative
        assert m.accounts["seller"].cash > 0
        assert m.accounts["seller"].position == -5

    def test_on_fill_callback_called(self):
        tracker = FillTracker("buyer", Side.BUY, 5)
        m = Market([tracker], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        assert len(tracker.fills) > 0

    def test_on_fill_correct_side(self):
        tracker = FillTracker("buyer", Side.BUY, 5)
        m = Market([tracker], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        assert tracker.fills[0][0] == Side.BUY


class TestMarketLeaderboard:
    def test_leaderboard_returns_non_seed_agents(self):
        agents = [PassiveAgent(f"a{i}", seed=i) for i in range(3)]
        m = Market(agents, n_ticks=3, seed=1)
        m.run(verbose_every=0)
        lb = m.leaderboard()
        ids = [agent_id for agent_id, _, _ in lb]
        assert "__seed__" not in ids
        assert len(lb) == 3

    def test_leaderboard_sorted_by_value(self):
        agents = [PassiveAgent(f"a{i}", seed=i) for i in range(3)]
        m = Market(agents, n_ticks=5, seed=1)
        m.run(verbose_every=0)
        lb = m.leaderboard()
        values = [v for _, v, _ in lb]
        assert values == sorted(values, reverse=True)

    def test_leaderboard_tuple_structure(self):
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=2, seed=1)
        m.run(verbose_every=0)
        agent_id, value, pos = m.leaderboard()[0]
        assert agent_id == "a"
        assert isinstance(value, float)
        assert isinstance(pos, int)


class TestMarketScenario:
    def test_at_tick_callback_fires(self):
        fired = []
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=10, seed=1)
        m.at_tick(5, lambda mkt: fired.append(True))
        m.run(verbose_every=0)
        assert fired == [True]

    def test_at_tick_fires_once(self):
        count = []
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=10, seed=1)
        m.at_tick(3, lambda mkt: count.append(1))
        m.run(verbose_every=0)
        assert sum(count) == 1

    def test_at_tick_receives_market(self):
        received = []
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=5, seed=1)
        m.at_tick(3, lambda mkt: received.append(mkt))
        m.run(verbose_every=0)
        assert len(received) == 1
        assert received[0] is m


class TestMarketTelemetry:
    def test_snapshots_recorded(self):
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        assert len(m.snapshots) == 5

    def test_snapshot_keys(self):
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=3, seed=1)
        m.run(verbose_every=0)
        snap = m.snapshots[0]
        for key in ("tick", "fundamental", "best_bid", "best_ask", "last_price", "volume"):
            assert key in snap

    def test_snapshot_tick_sequence(self):
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=4, seed=1)
        m.run(verbose_every=0)
        ticks = [s["tick"] for s in m.snapshots]
        assert ticks == [1, 2, 3, 4]

    def test_write_telemetry(self, tmp_path):
        agent = PassiveAgent("a", seed=0)
        m = Market([agent], n_ticks=5, seed=1)
        m.run(verbose_every=0)
        snaps = tmp_path / "snaps.csv"
        trades = tmp_path / "trades.csv"
        m.write_telemetry(str(snaps), str(trades))
        assert snaps.exists()
        assert trades.exists()
        assert snaps.stat().st_size > 0
