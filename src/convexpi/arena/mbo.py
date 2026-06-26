"""
mbo.py — reference market-by-order (L3) book + queue-position simulator.

This is the *realistic exchange* model, kept separate from the L2 discrete-tick engine in engine.py.
It reconstructs the book **order by order** from an L3 event stream (Bitstamp `live_orders` /
`live_trades`, recorded by deploy/fetch_crypto_l3.py), so it can answer the questions the snapshot
model can't:

  * Where am I in the FIFO queue at my price, and how fast does it drain?
  * Do I get filled — and was it adverse (price moved against me right as I filled)?
  * If I try to cancel, does my cancel beat the incoming trade (the latency race)?

This reference is deliberately readable Python — it *defines the semantics*. A faster C++/Rust core
can later implement the same interface for production-scale replay; see the note in mbo_demo.py.

Event records (from the recorder):
    {"k":"o","e":"created|changed|deleted","id":int,"p":price,"a":remaining,"s":0|1,"tr":traded,"t":microts}
    {"k":"t","p":price,"a":size,"s":0|1,"t":microts}     # trade; s = taker side (0 buy, 1 sell)
Side convention: 0 = buy/bid, 1 = sell/ask.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field


class L3Book:
    """Full order-by-order book: every resting order, with FIFO ordering per price level."""

    def __init__(self):
        self.orders: dict[int, list] = {}                       # id -> [price, amount, side]
        self.levels: dict[int, dict[float, list[int]]] = {0: {}, 1: {}}  # side -> price -> [id,...] (FIFO)

    def apply(self, ev: dict) -> None:
        e = ev["e"]
        if e == "created":
            self.orders[ev["id"]] = [ev["p"], ev["a"], ev["s"]]
            self.levels[ev["s"]].setdefault(ev["p"], []).append(ev["id"])
        elif e == "changed":
            o = self.orders.get(ev["id"])
            if o:
                o[1] = ev["a"]
        elif e == "deleted":
            o = self.orders.pop(ev["id"], None)
            if o:
                price, _, side = o
                lst = self.levels[side].get(price)
                if lst and ev["id"] in lst:
                    lst.remove(ev["id"])
                if lst is not None and not lst:
                    del self.levels[side][price]

    def best_bid(self) -> float | None:
        return max(self.levels[0]) if self.levels[0] else None

    def best_ask(self) -> float | None:
        return min(self.levels[1]) if self.levels[1] else None

    def clean_touch(self) -> tuple[float | None, float | None]:
        """Best (bid, ask) that aren't crossed. Reconstructing an L3 stream without an initial
        snapshot leaves a few stale orders that cross the book; this returns the uncrossed touch
        (the best ask, and the best bid strictly below it). Production replay should instead seed
        from an exchange L3 snapshot — see mbo_demo.py."""
        if not self.levels[0] or not self.levels[1]:
            return self.best_bid(), self.best_ask()
        ask = min(self.levels[1])
        bid = next((p for p in sorted(self.levels[0], reverse=True) if p < ask), None)
        return bid, ask

    def size_at(self, side: int, price: float) -> float:
        return sum(self.orders[i][1] for i in self.levels[side].get(price, []))

    def order_ids_at(self, side: int, price: float) -> list[int]:
        return list(self.levels[side].get(price, []))


@dataclass
class PassiveResult:
    """Outcome of resting a passive limit order, modelled order-by-order."""
    side: int
    price: float
    size: float
    enter_ts: int
    initial_queue_ahead: float
    filled: bool = False
    cancelled: bool = False
    fill_ts: int | None = None
    reached_front_ts: int | None = None
    adverse: bool | None = None              # did the mid move against us at the fill?
    queue_trace: list[tuple[int, float]] = field(default_factory=list)   # (ts, queue_ahead)

    @property
    def time_to_fill_s(self) -> float | None:
        return (self.fill_ts - self.enter_ts) / 1e6 if self.fill_ts else None


def simulate_passive_order(events: list[dict], side: int, price: float, enter_idx: int,
                           size: float, *, cancel_after_s: float | None = None,
                           latency_us: int = 0) -> PassiveResult:
    """Rest a limit order of `size` at `price` (side 0=buy/1=sell) at event index `enter_idx`,
    then replay the real L3 stream and track its FIFO queue position to fill/cancel.

    queue model: we record the live orders resting *ahead* of us at our price when we join. Each is
    removed (cancel or trade) order-by-order as the real stream plays out. Once nothing is ahead, the
    next trade at our price on the opposite (taker) side fills us. `cancel_after_s` models a maker
    pulling the quote; `latency_us` is how long our cancel takes to land — if a trade fills us inside
    that window, we were adversely selected before the cancel arrived.
    """
    book = L3Book()
    for ev in events[:enter_idx]:
        if ev["k"] == "o":
            book.apply(ev)

    ahead = set(book.order_ids_at(side, price))
    queue_ahead = book.size_at(side, price)
    enter_ts = events[enter_idx]["t"]
    res = PassiveResult(side=side, price=price, size=size, enter_ts=enter_ts,
                        initial_queue_ahead=queue_ahead)
    taker_opp = 1 - side                       # trades by this taker side consume our queue
    cancel_decided_ts = None
    filled_size = 0.0

    for ev in events[enter_idx:]:
        ts = ev["t"]
        # maker decides to pull the quote after holding it cancel_after_s
        if cancel_after_s is not None and cancel_decided_ts is None and (ts - enter_ts) / 1e6 >= cancel_after_s:
            cancel_decided_ts = ts
        # cancel lands after latency — if nothing filled us first, we're out
        if cancel_decided_ts is not None and ts >= cancel_decided_ts + latency_us and not res.filled:
            res.cancelled = True
            res.queue_trace.append((ts, max(0.0, queue_ahead)))
            break

        if ev["k"] == "o":
            if ev["e"] == "deleted" and ev["id"] in ahead:
                o = book.orders.get(ev["id"])
                if o:
                    queue_ahead -= o[1]
                ahead.discard(ev["id"])
            book.apply(ev)
        elif ev["k"] == "t" and ev["s"] == taker_opp and abs(ev["p"] - price) < 1e-9:
            if queue_ahead > 1e-12:
                queue_ahead -= ev["a"]          # trade eats the queue ahead of us
            else:
                if res.reached_front_ts is None:
                    res.reached_front_ts = ts
                filled_size += ev["a"]          # now it's filling us
                if filled_size >= size - 1e-12:
                    res.filled = True
                    res.fill_ts = ts
                    # adverse if the touch on our side moved away (price went against the maker)
                    bb, ba = book.best_bid(), book.best_ask()
                    if side == 0 and bb is not None:
                        res.adverse = bb < price          # we bought as bids fell below our price
                    elif side == 1 and ba is not None:
                        res.adverse = ba > price
                    res.queue_trace.append((ts, 0.0))
                    break
        res.queue_trace.append((ts, max(0.0, queue_ahead)))

    return res


def load_l3(path: str) -> list[dict]:
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]
