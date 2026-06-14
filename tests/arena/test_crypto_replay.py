"""Tests for CryptoFeed and CryptoReplayMarket."""

import csv
import tempfile
from pathlib import Path

import pytest

from convexpi.arena.crypto_replay import (
    CryptoFeed, CryptoReplayMarket, OHLCVBar, _save_csv, _load_csv,
)
from convexpi.arena.agents import Agent, MarketState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_bars(bars: list[OHLCVBar], path: Path) -> None:
    _save_csv(bars, path)


def _make_bars(n: int, start_price: float = 50_000.0) -> list[OHLCVBar]:
    """Generate synthetic bars with linearly increasing close prices."""
    return [
        OHLCVBar(
            timestamp_ms=1_700_000_000_000 + i * 60_000,
            open=start_price + i,
            high=start_price + i + 10,
            low=start_price + i - 10,
            close=start_price + i,
            volume=1.0 + i * 0.1,
        )
        for i in range(n)
    ]


class PassiveAgent(Agent):
    def on_tick(self, state: MarketState):
        return []


# ---------------------------------------------------------------------------
# CryptoFeed tests
# ---------------------------------------------------------------------------

class TestCryptoFeed:
    def test_loads_bars(self, tmp_path):
        bars = _make_bars(5)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        assert feed.n_bars == 5

    def test_step_returns_price_in_cents(self, tmp_path):
        bars = _make_bars(3, start_price=50_000.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        price = feed.step()
        assert price == bars[0].close * 100

    def test_step_advances_each_call(self, tmp_path):
        bars = _make_bars(3, start_price=100.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        prices = [feed.step() for _ in range(3)]
        assert prices[0] == 100.0 * 100
        assert prices[1] == 101.0 * 100
        assert prices[2] == 102.0 * 100

    def test_loop_wraps_around(self, tmp_path):
        bars = _make_bars(2, start_price=100.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100, loop=True)
        feed.step()  # bar 0
        feed.step()  # bar 1
        price = feed.step()  # wraps to bar 0
        assert price == bars[0].close * 100

    def test_no_loop_holds_last(self, tmp_path):
        bars = _make_bars(2, start_price=200.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100, loop=False)
        feed.step()  # bar 0
        feed.step()  # bar 1 (last)
        price = feed.step()  # holds bar 1
        assert price == bars[1].close * 100

    def test_metadata_fields(self, tmp_path):
        bars = _make_bars(5, start_price=30_000.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        meta = feed.metadata()
        assert meta["bars"] == 5
        assert meta["start_price"] == bars[0].close
        assert meta["end_price"] == bars[-1].close

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("timestamp_ms,open,high,low,close,volume\n")
        with pytest.raises(ValueError, match="No bars"):
            CryptoFeed(p)


# ---------------------------------------------------------------------------
# CSV round-trip
# ---------------------------------------------------------------------------

class TestCSVRoundTrip:
    def test_save_and_load(self, tmp_path):
        bars = _make_bars(10, start_price=42_000.0)
        p = tmp_path / "rt.csv"
        _save_csv(bars, p)
        loaded = _load_csv(p)
        assert len(loaded) == 10
        assert loaded[0].close == bars[0].close
        assert loaded[-1].timestamp_ms == bars[-1].timestamp_ms

    def test_sorted_on_load(self, tmp_path):
        bars = _make_bars(5)
        reversed_bars = list(reversed(bars))
        p = tmp_path / "rev.csv"
        _save_csv(reversed_bars, p)
        loaded = _load_csv(p)
        timestamps = [b.timestamp_ms for b in loaded]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# CryptoReplayMarket tests
# ---------------------------------------------------------------------------

class TestCryptoReplayMarket:
    def test_runs_n_ticks(self, tmp_path):
        bars = _make_bars(10, start_price=50_000.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        agents = [PassiveAgent("passive")]
        market = CryptoReplayMarket(agents, feed=feed, n_ticks=5)
        market.run()
        assert len(market.snapshots) == 5

    def test_price_tracks_feed(self, tmp_path):
        bars = _make_bars(5, start_price=100.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        agents = [PassiveAgent("passive")]
        market = CryptoReplayMarket(agents, feed=feed, n_ticks=5)
        market.run()
        # Fundamental values in snapshots should track bar close prices (in cents)
        for i, snap in enumerate(market.snapshots):
            expected = (100.0 + i) * 100
            assert abs(snap["fundamental"] - expected) < 1e-6

    def test_leaderboard_contains_agent(self, tmp_path):
        bars = _make_bars(3, start_price=50_000.0)
        p = tmp_path / "bars.csv"
        _write_bars(bars, p)
        feed = CryptoFeed(p, cents_per_unit=100)
        agents = [PassiveAgent("alice")]
        market = CryptoReplayMarket(agents, feed=feed, n_ticks=3)
        market.run()
        agent_ids = [r[0] for r in market.leaderboard()]
        assert "alice" in agent_ids
