"""Tests for the limit-order-book matching engine.

Engine API:
  OrderBook.submit(order, tick)  → list[Trade]  (also rests LIMIT remainder)
  OrderBook.cancel(order_id, agent_id) → bool
  OrderBook.depth(levels=5)      → {"bids": [(price, qty)...], "asks": [...]}
  OrderBook.best_bid() / best_ask()
  OrderBook.live                 → dict[order_id, Order]

  MatchingEngine.process_tick(tick, orders) → list[Trade]
  MatchingEngine.book  → OrderBook
  MatchingEngine.last_price
"""

import pytest
from convexpi.arena.engine import Order, OrderBook, MatchingEngine, Side, OrderType, Trade


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def limit(agent_id, side, price, qty):
    return Order(agent_id, side, qty, price=price)

def market_order(agent_id, side, qty):
    return Order(agent_id, side, qty, order_type=OrderType.MARKET)

def cancel_order(agent_id, order_id):
    return Order(agent_id, Side.BUY, 0, order_type=OrderType.CANCEL, cancel_id=order_id)


# ---------------------------------------------------------------------------
# OrderBook — direct API tests
# ---------------------------------------------------------------------------

class TestOrderBook:
    def test_empty_book(self):
        book = OrderBook()
        assert book.best_bid() is None
        assert book.best_ask() is None

    def test_single_bid_rests(self):
        book = OrderBook()
        o = limit("a", Side.BUY, 100, 10)
        trades = book.submit(o, tick=0)
        assert trades == []
        assert book.best_bid() == 100
        assert book.best_ask() is None

    def test_single_ask_rests(self):
        book = OrderBook()
        o = limit("a", Side.SELL, 105, 10)
        book.submit(o, tick=0)
        assert book.best_ask() == 105
        assert book.best_bid() is None

    def test_best_bid_is_highest(self):
        book = OrderBook()
        for p in [99, 101, 100]:
            book.submit(limit("a", Side.BUY, p, 5), tick=0)
        assert book.best_bid() == 101

    def test_best_ask_is_lowest(self):
        book = OrderBook()
        for p in [105, 103, 107]:
            book.submit(limit("a", Side.SELL, p, 5), tick=0)
        assert book.best_ask() == 103

    def test_cancel_removes_bid(self):
        book = OrderBook()
        o = limit("a", Side.BUY, 100, 10)
        book.submit(o, tick=0)
        assert book.best_bid() == 100
        removed = book.cancel(o.order_id, "a")
        assert removed is True
        assert book.best_bid() is None

    def test_cancel_wrong_agent_noop(self):
        book = OrderBook()
        o = limit("alice", Side.BUY, 100, 10)
        book.submit(o, tick=0)
        # bob tries to cancel alice's order — should be refused
        result = book.cancel(o.order_id, "bob")
        assert result is False
        assert book.best_bid() == 100  # still there

    def test_cancel_nonexistent_returns_false(self):
        book = OrderBook()
        assert book.cancel(999999, "anyone") is False

    def test_depth_bids_descending(self):
        book = OrderBook()
        for p in [100, 102, 101]:
            book.submit(limit("a", Side.BUY, p, 5), tick=0)
        bids = book.depth()["bids"]
        prices = [p for p, _ in bids]
        assert prices == sorted(prices, reverse=True)

    def test_depth_asks_ascending(self):
        book = OrderBook()
        for p in [105, 103, 107]:
            book.submit(limit("a", Side.SELL, p, 5), tick=0)
        asks = book.depth()["asks"]
        prices = [p for p, _ in asks]
        assert prices == sorted(prices)

    def test_depth_aggregates_same_price(self):
        book = OrderBook()
        book.submit(limit("a", Side.BUY, 100, 5), tick=0)
        book.submit(limit("b", Side.BUY, 100, 3), tick=0)
        bids = book.depth()["bids"]
        assert bids[0] == (100, 8)

    def test_live_tracks_resting_orders(self):
        book = OrderBook()
        o1 = limit("alice", Side.BUY, 100, 5)
        o2 = limit("alice", Side.SELL, 110, 3)
        o3 = limit("bob", Side.BUY, 99, 10)
        book.submit(o1, tick=0)
        book.submit(o2, tick=0)
        book.submit(o3, tick=0)
        alice_orders = [o for o in book.live.values() if o.agent_id == "alice"]
        assert len(alice_orders) == 2


# ---------------------------------------------------------------------------
# OrderBook — matching via submit()
# ---------------------------------------------------------------------------

class TestOrderBookMatching:
    def test_no_match_when_spread_positive(self):
        book = OrderBook()
        book.submit(limit("a", Side.BUY, 99, 10), tick=0)
        trades = book.submit(limit("b", Side.SELL, 101, 10), tick=0)
        assert trades == []

    def test_match_at_resting_price(self):
        book = OrderBook()
        book.submit(limit("seller", Side.SELL, 100, 10), tick=0)
        trades = book.submit(limit("buyer", Side.BUY, 105, 10), tick=1)
        assert len(trades) == 1
        t = trades[0]
        assert t.price == 100          # resting order price wins
        assert t.qty == 10
        assert t.buyer_id == "buyer"
        assert t.seller_id == "seller"

    def test_partial_fill_remainder_rests(self):
        book = OrderBook()
        book.submit(limit("seller", Side.SELL, 100, 5), tick=0)
        trades = book.submit(limit("buyer", Side.BUY, 100, 10), tick=1)
        assert len(trades) == 1
        assert trades[0].qty == 5
        # Remaining 5 should still be on the book as a bid
        assert book.best_bid() == 100

    def test_full_fill_clears_book(self):
        book = OrderBook()
        book.submit(limit("seller", Side.SELL, 100, 10), tick=0)
        book.submit(limit("buyer", Side.BUY, 100, 10), tick=1)
        assert book.best_ask() is None
        assert book.best_bid() is None

    def test_multiple_fills_one_aggressor(self):
        book = OrderBook()
        for i in range(3):
            book.submit(limit(f"s{i}", Side.SELL, 100, 5), tick=i)
        trades = book.submit(limit("buyer", Side.BUY, 100, 15), tick=10)
        assert len(trades) == 3
        assert sum(t.qty for t in trades) == 15

    def test_time_priority_same_price(self):
        book = OrderBook()
        # s1 arrives before s2 at the same price
        book.submit(limit("s1", Side.SELL, 100, 5), tick=1)
        book.submit(limit("s2", Side.SELL, 100, 5), tick=2)
        trades = book.submit(limit("buyer", Side.BUY, 100, 5), tick=3)
        assert trades[0].seller_id == "s1"   # FIFO: s1 first

    def test_best_price_priority(self):
        book = OrderBook()
        # s2 quotes lower (better) ask
        book.submit(limit("s1", Side.SELL, 102, 5), tick=1)
        book.submit(limit("s2", Side.SELL, 100, 5), tick=2)
        trades = book.submit(limit("buyer", Side.BUY, 105, 5), tick=3)
        assert trades[0].seller_id == "s2"
        assert trades[0].price == 100

    def test_market_buy_hits_best_ask(self):
        book = OrderBook()
        book.submit(limit("seller", Side.SELL, 100, 10), tick=0)
        trades = book.submit(market_order("buyer", Side.BUY, 5), tick=1)
        assert len(trades) == 1
        assert trades[0].price == 100
        assert trades[0].qty == 5

    def test_market_sell_hits_best_bid(self):
        book = OrderBook()
        book.submit(limit("buyer", Side.BUY, 99, 10), tick=0)
        trades = book.submit(market_order("seller", Side.SELL, 5), tick=1)
        assert len(trades) == 1
        assert trades[0].price == 99

    def test_market_order_no_liquidity_no_fill(self):
        book = OrderBook()
        trades = book.submit(market_order("buyer", Side.BUY, 10), tick=0)
        assert trades == []

    def test_market_order_sweeps_multiple_levels(self):
        book = OrderBook()
        for i, p in enumerate([100, 101, 102]):
            book.submit(limit(f"s{i}", Side.SELL, p, 3), tick=i)
        trades = book.submit(market_order("buyer", Side.BUY, 9), tick=10)
        assert sum(t.qty for t in trades) == 9

    def test_cancel_prevents_fill(self):
        book = OrderBook()
        o = limit("seller", Side.SELL, 100, 10)
        book.submit(o, tick=0)
        book.cancel(o.order_id, "seller")
        trades = book.submit(limit("buyer", Side.BUY, 100, 10), tick=1)
        assert trades == []


# ---------------------------------------------------------------------------
# MatchingEngine — process_tick()
# ---------------------------------------------------------------------------

class TestMatchingEngine:
    def setup_method(self):
        self.engine = MatchingEngine(seed=42)

    def test_single_order_rests(self):
        trades = self.engine.process_tick(1, [limit("a", Side.BUY, 99, 10)])
        assert trades == []
        assert self.engine.book.best_bid() == 99

    def test_two_orders_cross(self):
        self.engine.process_tick(1, [limit("seller", Side.SELL, 100, 5)])
        trades = self.engine.process_tick(2, [limit("buyer", Side.BUY, 100, 5)])
        assert len(trades) == 1
        assert trades[0].qty == 5

    def test_last_price_updated_on_trade(self):
        assert self.engine.last_price is None
        self.engine.process_tick(1, [limit("s", Side.SELL, 100, 5)])
        self.engine.process_tick(2, [limit("b", Side.BUY, 100, 5)])
        assert self.engine.last_price == 100

    def test_no_cross_no_last_price_change(self):
        self.engine.process_tick(1, [limit("a", Side.BUY, 99, 5)])
        self.engine.process_tick(2, [limit("b", Side.SELL, 101, 5)])
        assert self.engine.last_price is None

    def test_cancel_via_process_tick(self):
        self.engine.process_tick(1, [limit("seller", Side.SELL, 100, 10)])
        o_id = [o for o in self.engine.book.live.values() if o.agent_id == "seller"][0].order_id
        self.engine.process_tick(2, [cancel_order("seller", o_id)])
        trades = self.engine.process_tick(3, [limit("buyer", Side.BUY, 100, 10)])
        assert trades == []

    def test_time_priority_across_ticks(self):
        # s1 submitted before s2 → s1 should fill first
        self.engine.process_tick(1, [limit("s1", Side.SELL, 100, 5)])
        self.engine.process_tick(2, [limit("s2", Side.SELL, 100, 5)])
        trades = self.engine.process_tick(3, [limit("buyer", Side.BUY, 100, 5)])
        assert trades[0].seller_id == "s1"

    def test_all_trades_accumulated(self):
        self.engine.process_tick(1, [limit("s", Side.SELL, 100, 5)])
        self.engine.process_tick(2, [limit("b", Side.BUY, 100, 5)])
        assert len(self.engine.trades) == 1
