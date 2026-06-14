"""
engine.py — Discrete-time limit order book matching engine.

Design notes (pedagogy-first):
- Price-time priority, the real-world standard.
- Discrete ticks: agents submit orders during a tick; orders are shuffled
  (fair randomization) and processed sequentially. Simple, deterministic
  given a seed, and sufficient for teaching microstructure.
- Integer prices in "cents" to avoid float comparison bugs — itself a lesson.
"""

from __future__ import annotations
import itertools
import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    LIMIT = "limit"
    MARKET = "market"
    CANCEL = "cancel"


_order_ids = itertools.count(1)


@dataclass
class Order:
    agent_id: str
    side: Side
    qty: int
    price: Optional[int] = None          # cents; None for MARKET/CANCEL
    order_type: OrderType = OrderType.LIMIT
    cancel_id: Optional[int] = None      # for CANCEL orders
    order_id: int = field(default_factory=lambda: next(_order_ids))
    tick: int = -1                       # stamped by the engine

    def __post_init__(self):
        if self.order_type == OrderType.LIMIT and (self.price is None or self.price <= 0):
            raise ValueError("LIMIT order requires a positive integer price (cents)")
        if self.order_type == OrderType.CANCEL and self.cancel_id is None:
            raise ValueError("CANCEL order requires cancel_id")
        if self.order_type != OrderType.CANCEL and self.qty <= 0:
            raise ValueError("qty must be positive")


@dataclass
class Trade:
    tick: int
    price: int                # cents
    qty: int
    buyer_id: str
    seller_id: str
    aggressor_side: Side
    maker_order_id: int
    taker_order_id: int


class OrderBook:
    """Price-time priority book. Bids and asks are dicts price -> deque[Order]."""

    def __init__(self):
        self.bids: dict[int, deque[Order]] = {}
        self.asks: dict[int, deque[Order]] = {}
        self.live: dict[int, Order] = {}   # order_id -> resting order

    # ---- views ----------------------------------------------------------
    def best_bid(self) -> Optional[int]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[int]:
        return min(self.asks) if self.asks else None

    def depth(self, levels: int = 5) -> dict:
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])[:levels]
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])[:levels]
        return {
            "bids": [(p, sum(o.qty for o in q)) for p, q in bids],
            "asks": [(p, sum(o.qty for o in q)) for p, q in asks],
        }

    # ---- internals ------------------------------------------------------
    def _rest(self, order: Order):
        book = self.bids if order.side == Side.BUY else self.asks
        book.setdefault(order.price, deque()).append(order)
        self.live[order.order_id] = order

    def _pop_level_if_empty(self, side: Side, price: int):
        book = self.bids if side == Side.BUY else self.asks
        if price in book and not book[price]:
            del book[price]

    def cancel(self, order_id: int, agent_id: str) -> bool:
        order = self.live.get(order_id)
        if order is None or order.agent_id != agent_id:
            return False
        book = self.bids if order.side == Side.BUY else self.asks
        try:
            book[order.price].remove(order)
        except (KeyError, ValueError):
            return False
        self._pop_level_if_empty(order.side, order.price)
        del self.live[order_id]
        return True

    # ---- matching -------------------------------------------------------
    def submit(self, order: Order, tick: int) -> list[Trade]:
        """Match an incoming order against the book; rest any remainder (LIMIT)."""
        trades: list[Trade] = []
        is_buy = order.side == Side.BUY
        opposite = self.asks if is_buy else self.bids

        def best_opposite_price() -> Optional[int]:
            return (min(opposite) if opposite else None) if is_buy else (max(opposite) if opposite else None)

        def crosses(p: int) -> bool:
            if order.order_type == OrderType.MARKET:
                return True
            return (order.price >= p) if is_buy else (order.price <= p)

        remaining = order.qty
        while remaining > 0:
            p = best_opposite_price()
            if p is None or not crosses(p):
                break
            queue = opposite[p]
            maker = queue[0]
            fill = min(remaining, maker.qty)
            maker.qty -= fill
            remaining -= fill
            trades.append(Trade(
                tick=tick, price=p, qty=fill,
                buyer_id=order.agent_id if is_buy else maker.agent_id,
                seller_id=maker.agent_id if is_buy else order.agent_id,
                aggressor_side=order.side,
                maker_order_id=maker.order_id,
                taker_order_id=order.order_id,
            ))
            if maker.qty == 0:
                queue.popleft()
                del self.live[maker.order_id]
                self._pop_level_if_empty(Side.SELL if is_buy else Side.BUY, p)

        if remaining > 0 and order.order_type == OrderType.LIMIT:
            order.qty = remaining
            self._rest(order)
        return trades


class MatchingEngine:
    """Batch-processes one tick of orders with fair randomization."""

    def __init__(self, seed: int = 0):
        self.book = OrderBook()
        self.rng = random.Random(seed)
        self.trades: list[Trade] = []
        self.last_price: Optional[int] = None

    def process_tick(self, tick: int, orders: list[Order]) -> list[Trade]:
        self.rng.shuffle(orders)  # fair: no agent systematically goes first
        tick_trades: list[Trade] = []
        for order in orders:
            order.tick = tick
            if order.order_type == OrderType.CANCEL:
                self.book.cancel(order.cancel_id, order.agent_id)
                continue
            tick_trades.extend(self.book.submit(order, tick))
        if tick_trades:
            self.last_price = tick_trades[-1].price
        self.trades.extend(tick_trades)
        return tick_trades
