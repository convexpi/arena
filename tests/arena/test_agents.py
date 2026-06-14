"""Tests for Arena agents: AvellanedaStoikov, TWAPAgent, MeanReversionAgent."""

from __future__ import annotations
import pytest
from unittest.mock import MagicMock

from convexpi.arena.agents import (
    AvellanedaStoikov,
    TWAPAgent,
    MeanReversionAgent,
    MarketState,
)
from convexpi.arena.engine import Order, OrderType, Side


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _state(
    tick=100,
    best_bid=1000,
    best_ask=1010,
    last_price=1005,
    position=0,
    cash=100_000,
    recent_trades=None,
    open_orders=None,
):
    trade = MagicMock()
    trade.price = 1005
    return MarketState(
        tick=tick,
        best_bid=best_bid,
        best_ask=best_ask,
        last_price=last_price,
        depth={"bids": [(1000, 10)], "asks": [(1010, 10)]},
        recent_trades=recent_trades if recent_trades is not None else [trade] * 5,
        position=position,
        cash=cash,
        my_open_orders=open_orders if open_orders is not None else [],
    )


def _trade(price=1005, qty=5):
    t = MagicMock()
    t.price = price
    t.qty = qty
    return t


# ---------------------------------------------------------------------------
# AvellanedaStoikov
# ---------------------------------------------------------------------------

class TestAvellanedaStoikov:
    def _agent(self, **kwargs):
        defaults = dict(agent_id="as1", gamma=0.1, kappa=1.5, size=15,
                        max_inventory=300, horizon=500)
        defaults.update(kwargs)
        return AvellanedaStoikov(**defaults)

    def test_returns_orders_when_mid_present(self):
        agent = self._agent()
        orders = agent.on_tick(_state())
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT]
        assert len(limit_orders) == 2  # bid + ask

    def test_returns_empty_without_mid(self):
        agent = self._agent()
        s = _state(best_bid=None, best_ask=None, last_price=None)
        orders = agent.on_tick(s)
        assert orders == []

    def test_bid_below_ask(self):
        agent = self._agent()
        orders = agent.on_tick(_state())
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT]
        bids = [o for o in limit_orders if o.side == Side.BUY]
        asks = [o for o in limit_orders if o.side == Side.SELL]
        assert bids and asks
        assert bids[0].price < asks[0].price

    def test_long_inventory_skews_ask_down(self):
        """Long position should push reservation price down (tighter ask)."""
        agent_flat = self._agent()
        agent_long = self._agent()
        neutral = _state(position=0)
        long_pos = _state(position=150)
        orders_flat = [o for o in agent_flat.on_tick(neutral)
                       if o.order_type == OrderType.LIMIT and o.side == Side.SELL]
        orders_long = [o for o in agent_long.on_tick(long_pos)
                       if o.order_type == OrderType.LIMIT and o.side == Side.SELL]
        # ask should be lower (or equal) when long
        assert orders_long[0].price <= orders_flat[0].price

    def test_no_bid_beyond_max_inventory(self):
        agent = self._agent(max_inventory=100)
        orders = agent.on_tick(_state(position=100))
        limit_bids = [o for o in orders
                      if o.order_type == OrderType.LIMIT and o.side == Side.BUY]
        assert len(limit_bids) == 0

    def test_no_ask_beyond_max_short(self):
        agent = self._agent(max_inventory=100)
        orders = agent.on_tick(_state(position=-100))
        limit_asks = [o for o in orders
                      if o.order_type == OrderType.LIMIT and o.side == Side.SELL]
        assert len(limit_asks) == 0

    def test_cancels_existing_orders(self):
        agent = self._agent()
        open_orders = [(42, Side.BUY, 1000, 15)]
        s = _state(open_orders=open_orders)
        orders = agent.on_tick(s)
        cancels = [o for o in orders if o.order_type == OrderType.CANCEL]
        assert len(cancels) == 1
        assert cancels[0].cancel_id == 42

    def test_accumulates_vol_window(self):
        agent = self._agent()
        trades = [_trade(1000 + i) for i in range(10)]
        agent.on_tick(_state(recent_trades=trades))
        assert len(agent._vol_window) > 0

    def test_vol_window_capped_at_50(self):
        agent = self._agent()
        for _ in range(10):
            agent.on_tick(_state(recent_trades=[_trade(1000 + i) for i in range(10)]))
        assert len(agent._vol_window) <= 50


# ---------------------------------------------------------------------------
# TWAPAgent
# ---------------------------------------------------------------------------

class TestTWAPAgent:
    def _agent(self, **kwargs):
        defaults = dict(agent_id="tw1", target_qty=100, duration=10, use_limit=True)
        defaults.update(kwargs)
        return TWAPAgent(**defaults)

    def test_places_limit_order_each_tick(self):
        agent = self._agent(use_limit=True)
        orders = agent.on_tick(_state())
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT]
        assert len(limit_orders) == 1

    def test_uses_market_order_when_flagged(self):
        agent = self._agent(use_limit=False)
        orders = agent.on_tick(_state())
        mkt_orders = [o for o in orders if o.order_type == OrderType.MARKET]
        assert len(mkt_orders) == 1

    def test_buy_side_for_positive_qty(self):
        agent = self._agent(target_qty=100)
        orders = agent.on_tick(_state(best_bid=1000, best_ask=1010))
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT]
        assert limit_orders[0].side == Side.BUY

    def test_sell_side_for_negative_qty(self):
        agent = self._agent(target_qty=-100)
        orders = agent.on_tick(_state(best_bid=1000, best_ask=1010))
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT]
        assert limit_orders[0].side == Side.SELL

    def test_stops_after_full_execution(self):
        agent = self._agent(target_qty=20, duration=4)
        # Fill everything
        agent._executed = 20
        orders = agent.on_tick(_state())
        assert orders == []

    def test_child_qty_does_not_exceed_remaining(self):
        agent = self._agent(target_qty=100, duration=10)
        agent._executed = 95  # only 5 left
        orders = agent.on_tick(_state())
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT]
        assert limit_orders[0].qty == 5

    def test_on_fill_accumulates_executed(self):
        agent = self._agent(target_qty=100, duration=10)
        agent.on_fill(MagicMock(), Side.BUY, 10, 1000)
        agent.on_fill(MagicMock(), Side.BUY, 10, 1001)
        assert agent._executed == 20

    def test_buy_limit_at_best_ask(self):
        agent = self._agent(target_qty=50, use_limit=True)
        orders = agent.on_tick(_state(best_ask=1010))
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT and o.side == Side.BUY]
        assert limit_orders[0].price == 1010

    def test_sell_limit_at_best_bid(self):
        agent = self._agent(target_qty=-50, use_limit=True)
        orders = agent.on_tick(_state(best_bid=1000))
        limit_orders = [o for o in orders if o.order_type == OrderType.LIMIT and o.side == Side.SELL]
        assert limit_orders[0].price == 1000

    def test_market_fallback_when_no_spread(self):
        agent = self._agent(use_limit=True)
        orders = agent.on_tick(_state(best_bid=None, best_ask=None))
        mkt_orders = [o for o in orders if o.order_type == OrderType.MARKET]
        assert len(mkt_orders) == 1


# ---------------------------------------------------------------------------
# MeanReversionAgent
# ---------------------------------------------------------------------------

class TestMeanReversionAgent:
    def _agent(self, **kwargs):
        defaults = dict(agent_id="mr1", lookback=10, entry_bps=20,
                        exit_bps=5, size=10, max_pos=300)
        defaults.update(kwargs)
        return MeanReversionAgent(**defaults)

    def test_no_orders_during_warmup(self):
        agent = self._agent(lookback=10)
        for _ in range(9):
            orders = agent.on_tick(_state())
        assert orders == []  # not enough history yet

    def test_no_orders_within_band(self):
        agent = self._agent(lookback=5, entry_bps=50)
        # Feed identical prices — deviation is 0
        for _ in range(5):
            agent.on_tick(_state(best_bid=1000, best_ask=1002, last_price=1001))
        orders = agent.on_tick(_state(best_bid=1000, best_ask=1002, last_price=1001))
        assert orders == []

    def test_sells_when_price_above_mean(self):
        agent = self._agent(lookback=5, entry_bps=20)
        base = 1000
        # Populate history with low prices
        for _ in range(5):
            agent.on_tick(_state(best_bid=base-1, best_ask=base+1, last_price=base))
        # Now spike price far above mean
        high = int(base * 1.01)  # +100 bps above
        orders = agent.on_tick(_state(best_bid=high-1, best_ask=high+1, last_price=high))
        sell_orders = [o for o in orders if o.side == Side.SELL]
        assert len(sell_orders) > 0

    def test_buys_when_price_below_mean(self):
        agent = self._agent(lookback=5, entry_bps=20)
        base = 1000
        for _ in range(5):
            agent.on_tick(_state(best_bid=base-1, best_ask=base+1, last_price=base))
        low = int(base * 0.99)  # -100 bps below
        orders = agent.on_tick(_state(best_bid=low-1, best_ask=low+1, last_price=low))
        buy_orders = [o for o in orders if o.side == Side.BUY]
        assert len(buy_orders) > 0

    def test_unwinds_long_on_reversion(self):
        agent = self._agent(lookback=5, entry_bps=20, exit_bps=5)
        base = 1000
        agent._history = [float(base)] * 5
        agent._history.append(float(base))  # ensure full window
        agent._history = agent._history[-5:]
        # Force a position
        agent_state = _state(best_bid=base-1, best_ask=base+1, last_price=base, position=50)
        orders = agent.on_tick(agent_state)
        sell_orders = [o for o in orders if o.side == Side.SELL]
        assert len(sell_orders) > 0  # unwinding long

    def test_unwinds_short_on_reversion(self):
        agent = self._agent(lookback=5, entry_bps=20, exit_bps=5)
        base = 1000
        agent._history = [float(base)] * 5
        agent_state = _state(best_bid=base-1, best_ask=base+1, last_price=base, position=-50)
        orders = agent.on_tick(agent_state)
        buy_orders = [o for o in orders if o.side == Side.BUY]
        assert len(buy_orders) > 0  # unwinding short

    def test_max_position_limit(self):
        agent = self._agent(lookback=5, entry_bps=20, max_pos=100)
        # At max long — should not add more buys even when price is low
        base = 1000
        for _ in range(5):
            agent.on_tick(_state(last_price=base))
        low = int(base * 0.99)
        orders = agent.on_tick(_state(last_price=low, position=100))
        buy_orders = [o for o in orders if o.side == Side.BUY]
        assert len(buy_orders) == 0

    def test_returns_empty_without_mid(self):
        agent = self._agent()
        orders = agent.on_tick(_state(best_bid=None, best_ask=None, last_price=None))
        assert orders == []
