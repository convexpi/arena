"""
crypto_book_replay.py — Replay a *real* limit order book through the Arena engine.

Where ``crypto_replay.py`` drives only the price *level* from OHLCV bars and lets synthetic
background agents manufacture the depth, this module replays **recorded L2 depth snapshots**: the
real bids and asks of a live exchange become the resting liquidity students trade against. A student
market order therefore walks the *actual* ask ladder and pays *actual* slippage; a student limit
order sits in a realistic queue and is filled when the real market trades through its price.

How it plugs in
---------------
``CryptoBookReplayMarket`` subclasses :class:`~convexpi.arena.market.Market` and overrides the two
hooks the tick loop already calls every tick (in both ``Market.run`` and the WebSocket server):

* ``_seed_book``           — load the first snapshot.
* ``_inject_fundamental``  — refresh the exchange liquidity from the next snapshot.

Exchange liquidity is held under the ``__seed__`` account (already excluded from the leaderboard).
Each tick we cancel the old exchange orders and *submit* the new snapshot through the real matching
engine, so any resting student/agent order the market has moved through gets filled — the snapshot
itself is never crossed, so the re-seed never matches exchange-against-exchange.

Data format (JSONL, one snapshot per line; prices in quote currency, sizes in base units)::

    {"t": 1700000000000, "b": [[36201.5, 1.20], [36200.0, 3.40]], "a": [[36202.0, 0.80]]}

Record it with ``deploy/fetch_crypto_orderbook.py`` (Binance/Coinbase public depth — no API key).

Usage::

    from convexpi.arena.crypto_book_replay import CryptoBookFeed, CryptoBookReplayMarket
    feed = CryptoBookFeed("data/btcusdt_book.jsonl", cents_per_unit=100, qty_scale=1000)
    market = CryptoBookReplayMarket(agents, feed=feed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .engine import Order, Side
from .market import Account, Market

# Exchange liquidity is booked under the existing seeder id, which the server and leaderboard
# already exclude from rankings.
BOOK_MAKER = "__seed__"


# ---------------------------------------------------------------------------
# Snapshot data type
# ---------------------------------------------------------------------------

@dataclass
class BookFrame:
    """One L2 depth snapshot, already converted to engine units (integer cents / integer size)."""
    timestamp_ms: int
    bids: list[tuple[int, int]]    # (price_cents, size_units), best first
    asks: list[tuple[int, int]]    # (price_cents, size_units), best first

    def mid_cents(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0][0] + self.asks[0][0]) / 2


# ---------------------------------------------------------------------------
# CryptoBookFeed — iterates recorded snapshots
# ---------------------------------------------------------------------------

class CryptoBookFeed:
    """
    Iterates recorded L2 snapshots. ``step()`` advances one snapshot and returns the mid price in
    cents (so it slots into the existing ``fundamental.step()`` call); ``frame()`` returns the
    current snapshot in engine units for the market to re-seed the book.

    Parameters
    ----------
    path : str | Path
        JSONL file: one ``{"t", "b", "a"}`` object per line (raw quote price + base size).
    cents_per_unit : float
        Price multiplier to integer cents (100 for USD-quoted symbols).
    qty_scale : float
        Size multiplier to integer engine units. e.g. 1000 means 1 unit = 0.001 BTC.
    levels : int | None
        Cap the number of book levels per side (None = keep all recorded).
    loop : bool
        Wrap to the start when snapshots are exhausted (for sessions longer than the recording).
    """

    def __init__(self, path: str | Path, *, cents_per_unit: float = 100,
                 qty_scale: float = 1000, levels: int | None = None, loop: bool = True):
        self._raw = _load_jsonl(path)
        if not self._raw:
            raise ValueError(f"No book snapshots loaded from {path}")
        self._cents = cents_per_unit
        self._qty = qty_scale
        self._levels = levels
        self._loop = loop
        self._idx = 0
        self._cur = self._frame_at(0)
        self.value = self._cur.mid_cents() or (self._cur.bids[0][0] if self._cur.bids else 0.0)

    # -- conversion -------------------------------------------------------
    def _frame_at(self, i: int) -> BookFrame:
        t, b, a = self._raw[i]

        def conv(levels):
            out = []
            for price, size in levels:
                pc = round(price * self._cents)
                su = round(size * self._qty)
                if pc > 0 and su > 0:
                    out.append((pc, su))
            return out[: self._levels] if self._levels else out

        return BookFrame(timestamp_ms=t, bids=conv(b), asks=conv(a))

    # -- iteration --------------------------------------------------------
    def step(self) -> float:
        self._cur = self._frame_at(self._idx)
        mid = self._cur.mid_cents()
        if mid is not None:
            self.value = mid
        self._idx += 1
        if self._idx >= len(self._raw):
            self._idx = 0 if self._loop else len(self._raw) - 1
        return self.value

    def frame(self) -> BookFrame:
        return self._cur

    @property
    def n_frames(self) -> int:
        return len(self._raw)

    def metadata(self) -> dict:
        first = self._frame_at(0)
        last = self._frame_at(len(self._raw) - 1)
        return {
            "frames": len(self._raw),
            "start_ms": first.timestamp_ms,
            "end_ms": last.timestamp_ms,
            "start_mid": first.mid_cents(),
            "end_mid": last.mid_cents(),
            "levels_per_side": max(len(first.bids), len(first.asks)),
        }


# ---------------------------------------------------------------------------
# CryptoBookReplayMarket — Market driven by a real recorded book
# ---------------------------------------------------------------------------

class CryptoBookReplayMarket(Market):
    """Market whose resting liquidity *is* a recorded real order book, refreshed every tick."""

    def __init__(self, agents, *, feed: CryptoBookFeed, n_ticks: int | None = None, seed: int = 0):
        n = n_ticks if n_ticks is not None else feed.n_frames
        super().__init__(agents, n_ticks=n, seed=seed)
        self.fundamental = feed              # type: ignore[assignment]
        self.accounts.setdefault(BOOK_MAKER, Account())
        self._replay_tick = 0

    # -- hooks the tick loop already calls --------------------------------
    def _seed_book(self):
        """Install the first snapshot as the opening book."""
        self._refresh_exchange_liquidity()

    def _inject_fundamental(self, fv: float) -> None:
        """Refresh the real book each tick (and let any background agents see the mid)."""
        super()._inject_fundamental(fv)
        self._refresh_exchange_liquidity()

    # -- the core: replace exchange depth with the recorded snapshot -------
    def _refresh_exchange_liquidity(self) -> None:
        self._replay_tick += 1
        book = self.engine.book
        frame = self.fundamental.frame()      # type: ignore[attr-defined]

        # 1. Cancel the previous tick's exchange orders (leave student/agent orders resting).
        for oid in [oid for oid, o in list(book.live.items()) if o.agent_id == BOOK_MAKER]:
            book.cancel(oid, BOOK_MAKER)

        # 2. Submit the new snapshot. A non-crossed snapshot only ever matches resting
        #    student/agent orders the market has traded through; the remainder rests as the book.
        fills = []
        for price, size in frame.bids:
            fills += book.submit(Order(BOOK_MAKER, Side.BUY, size, price=price), tick=self._replay_tick)
        for price, size in frame.asks:
            fills += book.submit(Order(BOOK_MAKER, Side.SELL, size, price=price), tick=self._replay_tick)

        # 3. Settle any fills against participants (book maker is excluded from the leaderboard).
        if fills:
            self._settle(fills)
            self.engine.trades.extend(fills)
            self.engine.last_price = fills[-1].price


# ---------------------------------------------------------------------------
# JSONL I/O
# ---------------------------------------------------------------------------

def _load_jsonl(path: str | Path) -> list[tuple[int, list, list]]:
    out: list[tuple[int, list, list]] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append((int(d["t"]), d.get("b", []), d.get("a", [])))
    out.sort(key=lambda r: r[0])
    return out


def save_jsonl(frames: list[dict], path: str | Path) -> None:
    """Append-safe writer for recorded snapshots. Each frame is ``{"t", "b", "a"}`` with raw
    (quote-price, base-size) pairs."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for fr in frames:
            f.write(json.dumps(fr, separators=(",", ":")) + "\n")
