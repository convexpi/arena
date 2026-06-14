"""
crypto_replay.py — Replay historical crypto prices through the Arena engine.

Instead of a synthetic jump-diffusion FundamentalValue, a CryptoFeed drives
the price level from pre-recorded OHLCV data. The LOB, background agents, and
student participation are all unchanged — students are marginal participants
anchored to real market prices.

Data source (default): Binance public klines API — no API key required.
  https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1000

Pre-recorded data is stored as a minimal CSV:
    timestamp_ms,open,high,low,close,volume
    1700000000000,36201.5,36215.0,36195.0,36210.0,12.34

Usage::

    from convexpi.arena.crypto_replay import CryptoFeed, load_binance_klines

    # Fetch live and cache
    load_binance_klines("BTCUSDT", limit=500, out_path="data/btcusdt.csv")

    # Use in the server
    feed = CryptoFeed("data/btcusdt.csv", cents_per_unit=100)
    market = CryptoReplayMarket(agents, feed=feed)
"""

from __future__ import annotations

import csv
import json
import math
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .market import Market


# ---------------------------------------------------------------------------
# Snapshot data type
# ---------------------------------------------------------------------------

@dataclass
class OHLCVBar:
    timestamp_ms: int
    open: float       # price (e.g. USD)
    high: float
    low: float
    close: float
    volume: float


# ---------------------------------------------------------------------------
# CryptoFeed — wraps a list of bars, step() returns the next price in cents
# ---------------------------------------------------------------------------

class CryptoFeed:
    """
    Iterates through pre-recorded OHLCV bars. Each call to step() advances
    one bar and returns the close price converted to integer cents so it is
    compatible with the existing LOB engine.

    Parameters
    ----------
    path : str | Path
        CSV with columns: timestamp_ms, open, high, low, close, volume
    cents_per_unit : float
        Multiplier to convert the raw price to cents.
        BTCUSDT prices are already in USD — multiply by 1 to keep dollars,
        or by 100 to work in cents. The engine expects cents, so use
        cents_per_unit=100 for full-dollar symbols and 1 for already-cent symbols.
    loop : bool
        If True, wrap around to the beginning when data is exhausted.
        Useful for classroom sessions longer than the recorded history.
    """

    def __init__(self, path: str | Path, *, cents_per_unit: float = 100,
                 loop: bool = True):
        self._bars = _load_csv(path)
        if not self._bars:
            raise ValueError(f"No bars loaded from {path}")
        self._cents_per_unit = cents_per_unit
        self._loop = loop
        self._idx = 0
        # Expose current value attribute so Market._inject_fundamental works
        self.value = self._bars[0].close * cents_per_unit

    def step(self) -> float:
        bar = self._bars[self._idx]
        self.value = bar.close * self._cents_per_unit
        self._idx += 1
        if self._idx >= len(self._bars):
            if self._loop:
                self._idx = 0
            else:
                self._idx = len(self._bars) - 1  # hold last price
        return self.value

    @property
    def n_bars(self) -> int:
        return len(self._bars)

    def metadata(self) -> dict:
        if not self._bars:
            return {}
        first, last = self._bars[0], self._bars[-1]
        return {
            "bars": len(self._bars),
            "start_ms": first.timestamp_ms,
            "end_ms": last.timestamp_ms,
            "start_price": first.close,
            "end_price": last.close,
        }


# ---------------------------------------------------------------------------
# CryptoReplayMarket — Market with a CryptoFeed as the fundamental
# ---------------------------------------------------------------------------

class CryptoReplayMarket(Market):
    """
    Subclass of Market that replaces the synthetic FundamentalValue with a
    CryptoFeed. The tick loop, LOB, and agent API are unchanged.

    The book is seeded around the first bar's close price rather than the
    default 10,000 cents. All other mechanics — background agents, risk,
    server integration — work without modification.
    """

    def __init__(self, agents, *, feed: CryptoFeed, n_ticks: int | None = None,
                 seed: int = 0):
        n = n_ticks if n_ticks is not None else feed.n_bars
        super().__init__(agents, n_ticks=n, seed=seed)
        # Replace the synthetic fundamental with the live feed
        self.fundamental = feed   # type: ignore[assignment]

    def _seed_book(self):
        """Seed book around the first bar's price rather than ~10,000."""
        v = round(self.fundamental.value)
        seeder = "__seed__"
        self.accounts[seeder] = __import__(
            'convexpi.arena.market', fromlist=['Account']
        ).Account()
        from .engine import Order, Side
        for i in range(1, 11):
            self.market_engine.book.submit(
                Order(seeder, Side.BUY, 50, price=v - 2 * i), tick=0
            )
            self.market_engine.book.submit(
                Order(seeder, Side.SELL, 50, price=v + 2 * i), tick=0
            )
        self.market_engine.last_price = v

    def _seed_book(self):  # noqa: F811 — intentional override
        """Seed book around the first bar's price."""
        from .engine import Order, Side
        from .market import Account
        v = round(self.fundamental.value)
        seeder = "__seed__"
        self.accounts[seeder] = Account()
        for i in range(1, 11):
            self.engine.book.submit(Order(seeder, Side.BUY, 50, price=v - 2 * i), tick=0)
            self.engine.book.submit(Order(seeder, Side.SELL, 50, price=v + 2 * i), tick=0)
        self.engine.last_price = v


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def load_binance_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    limit: int = 1000,
    *,
    out_path: str | Path | None = None,
) -> list[OHLCVBar]:
    """
    Fetch up to `limit` 1-minute klines from Binance public API (no auth).
    Optionally saves to CSV at `out_path`.

    Returns a list of OHLCVBar sorted oldest-first.
    """
    url = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "convexpi/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        raw = json.loads(resp.read())

    bars = [
        OHLCVBar(
            timestamp_ms=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in raw
    ]

    if out_path is not None:
        _save_csv(bars, out_path)

    return bars


def load_coinbase_candles(
    product_id: str = "BTC-USD",
    granularity: int = 60,
    limit: int = 300,
    *,
    out_path: str | Path | None = None,
) -> list[OHLCVBar]:
    """
    Fetch candles from Coinbase Advanced Trade public API (no auth for public data).
    `granularity` is in seconds (60 = 1-minute candles).

    Returns a list of OHLCVBar sorted oldest-first.
    """
    url = (
        f"https://api.coinbase.com/api/v3/brokerage/market/products"
        f"/{product_id}/candles?granularity=ONE_MINUTE&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "convexpi/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())

    candles = data.get("candles", [])
    bars = sorted(
        [
            OHLCVBar(
                timestamp_ms=int(c["start"]) * 1000,
                open=float(c["open"]),
                high=float(c["high"]),
                low=float(c["low"]),
                close=float(c["close"]),
                volume=float(c["volume"]),
            )
            for c in candles
        ],
        key=lambda b: b.timestamp_ms,
    )

    if out_path is not None:
        _save_csv(bars, out_path)

    return bars


# ---------------------------------------------------------------------------
# CSV I/O
# ---------------------------------------------------------------------------

_FIELDNAMES = ["timestamp_ms", "open", "high", "low", "close", "volume"]


def _save_csv(bars: list[OHLCVBar], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        w.writeheader()
        for b in bars:
            w.writerow({
                "timestamp_ms": b.timestamp_ms,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            })


def _load_csv(path: str | Path) -> list[OHLCVBar]:
    bars = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            bars.append(OHLCVBar(
                timestamp_ms=int(row["timestamp_ms"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return sorted(bars, key=lambda b: b.timestamp_ms)
