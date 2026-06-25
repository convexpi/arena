"""Tests for CryptoBookFeed and CryptoBookReplayMarket (real L2 order-book replay)."""

import pytest

from convexpi.arena.crypto_book_replay import (
    CryptoBookFeed, CryptoBookReplayMarket, save_jsonl, _load_jsonl,
)
from convexpi.arena.engine import Order, Side, OrderType
from convexpi.arena.agents import Agent, MarketState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_frames(n: int):
    """n snapshots; mid drifts up by $1/frame. Bids/asks two levels each side, sizes in base units."""
    frames = []
    for i in range(n):
        mid = 100.0 + i
        frames.append({
            "t": 1_700_000_000_000 + i * 1000,
            "b": [[mid - 0.5, 1.0], [mid - 1.5, 2.0]],
            "a": [[mid + 0.5, 1.0], [mid + 1.5, 2.0]],
        })
    return frames


class PassiveAgent(Agent):
    def on_tick(self, state: MarketState):
        return []


class MarketBuyOnce(Agent):
    """Sends a single market buy of `qty` units on the first tick, then rests."""
    def __init__(self, agent_id, qty):
        super().__init__(agent_id)
        self.qty = qty
        self._done = False

    def on_tick(self, state: MarketState):
        if self._done:
            return []
        self._done = True
        return [Order(self.agent_id, Side.BUY, self.qty, order_type=OrderType.MARKET)]


# ---------------------------------------------------------------------------
# CryptoBookFeed
# ---------------------------------------------------------------------------

class TestCryptoBookFeed:
    def test_loads_and_converts(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(3), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000)
        assert feed.n_frames == 3
        f0 = feed.frame()
        # best bid 99.5 -> 9950 cents, size 1.0 -> 1000 units
        assert f0.bids[0] == (9950, 1000)
        assert f0.asks[0] == (10050, 1000)

    def test_step_returns_mid_cents_and_advances(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(3), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000)
        assert feed.step() == 100.0 * 100          # frame 0 mid
        assert feed.step() == 101.0 * 100          # frame 1 mid
        assert feed.step() == 102.0 * 100          # frame 2 mid

    def test_loop_wraps(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(2), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000, loop=True)
        feed.step(); feed.step()
        assert feed.step() == 100.0 * 100          # wrapped to frame 0

    def test_levels_cap(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(1), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000, levels=1)
        assert len(feed.frame().bids) == 1
        assert len(feed.frame().asks) == 1

    def test_empty_raises(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        with pytest.raises(ValueError, match="No book snapshots"):
            CryptoBookFeed(p)

    def test_metadata(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(4), p)
        meta = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000).metadata()
        assert meta["frames"] == 4
        assert meta["levels_per_side"] == 2


# ---------------------------------------------------------------------------
# JSONL round-trip
# ---------------------------------------------------------------------------

class TestJsonlRoundTrip:
    def test_save_load_sorted(self, tmp_path):
        frames = _make_frames(5)
        p = tmp_path / "rt.jsonl"
        save_jsonl(list(reversed(frames)), p)
        loaded = _load_jsonl(p)
        assert len(loaded) == 5
        assert [r[0] for r in loaded] == sorted(r[0] for r in loaded)


# ---------------------------------------------------------------------------
# CryptoBookReplayMarket
# ---------------------------------------------------------------------------

class TestCryptoBookReplayMarket:
    def test_book_reflects_real_snapshot(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(3), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000)
        m = CryptoBookReplayMarket([PassiveAgent("p")], feed=feed, n_ticks=1)
        m._seed_book()
        assert m.engine.book.best_bid() == 9950
        assert m.engine.book.best_ask() == 10050

    def test_market_order_walks_real_depth(self, tmp_path):
        # Buy 1.5 base units (1500 scaled): fills 1000 @ 10050 then 500 @ 10150 (real slippage).
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(3), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000)
        agent = MarketBuyOnce("taker", qty=1500)
        m = CryptoBookReplayMarket([agent], feed=feed, n_ticks=1)
        m.run()
        taker_trades = [t for t in m.engine.trades if t.buyer_id == "taker"]
        assert sum(t.qty for t in taker_trades) == 1500
        prices = sorted(t.price for t in taker_trades)
        assert prices == [10050, 10150]            # walked two levels of the real ladder

    def test_runs_and_leaderboard(self, tmp_path):
        p = tmp_path / "book.jsonl"
        save_jsonl(_make_frames(6), p)
        feed = CryptoBookFeed(p, cents_per_unit=100, qty_scale=1000)
        m = CryptoBookReplayMarket([PassiveAgent("alice")], feed=feed, n_ticks=4)
        m.run()
        assert len(m.snapshots) == 4
        assert "alice" in [r[0] for r in m.leaderboard()]
        assert "__seed__" not in [r[0] for r in m.leaderboard()]
