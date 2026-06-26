"""
crypto_l3_replay.py — a LIVE arena market driven by a real order-by-order (L3) stream.

This is the playable side of the realistic exchange. Where crypto_book_replay.py re-seeds aggregated
L2 depth every tick (so queue position is meaningless), this replays the recorded L3 message stream
(deploy/fetch_crypto_l3.py) into the matching engine's existing **price-time FIFO book**, so:

  * real orders rest in arrival order — a connected agent's limit order joins the **back of the real
    queue** at its price, and only advances as the real orders ahead cancel;
  * real **trades consume the front of the queue**, so an agent fills only once it has reached the
    front — genuine queue-based fills, not snapshot magic.

It reuses Market/MatchingEngine unchanged (the FIFO deque per price level is what makes queue position
real). Latency is approximated at tick granularity here; the continuous-time latency/cancel race lives
in the reference simulator (mbo.py), which is the semantics oracle these fills are conformance-tested
against (see tests/arena/test_l3_replay.py).
"""
from __future__ import annotations

from .engine import Order, OrderType, Side, Trade
from .market import Account, Market
from .mbo import load_l3

BOOK = "__seed__"   # real liquidity is booked here (already excluded from rankings)


class _Mid:
    """Trivial 'fundamental' so the Market tick loop's fundamental.step() has something to call."""
    def __init__(self):
        self.value = 0.0

    def step(self) -> float:
        return self.value


class MboReplayMarket(Market):
    def __init__(self, agents, *, l3_path: str, cents_per_unit: float = 100,
                 qty_scale: float = 1_000_000, events_per_tick: int = 150,
                 warmup_events: int = 3000, latency_us: int = 0,
                 n_ticks: int | None = None, seed: int = 0):
        self._events = load_l3(l3_path)
        if not self._events:
            raise ValueError(f"no L3 events in {l3_path}")
        avail = max(1, len(self._events) - warmup_events)
        n = n_ticks if n_ticks is not None else max(1, avail // events_per_tick)
        super().__init__(agents, n_ticks=n, seed=seed)
        self.fundamental = _Mid()                 # type: ignore[assignment]
        self.accounts.setdefault(BOOK, Account())
        self._cents = cents_per_unit
        self._qty = qty_scale
        # Public: quantities are integer micro-units (qty_scale per natural unit), so
        # raw PnL (cents x qty_scale) must be divided by this to recover real cents.
        self.qty_scale = qty_scale
        self._eptick = events_per_tick
        self._warmup = warmup_events
        self._latency_us = latency_us             # order-entry latency (the cancel-race clock)
        self._cursor = 0
        self._idmap: dict[int, Order] = {}        # bitstamp order id -> resting engine Order
        self._tick = 0
        self._clock = 0                           # replay clock = timestamp of last L3 event
        self._pending: list[tuple[int, Order]] = []   # (land_time_us, agent order) awaiting latency
        self._agent_ids = {a.agent_id for a in agents}

    # -- conversions ------------------------------------------------------
    def _px(self, p: float) -> int:
        return max(1, round(p * self._cents))

    def _qy(self, a: float) -> int:
        return max(1, round(a * self._qty))

    # -- applying the L3 stream ------------------------------------------
    def _apply(self, ev: dict) -> list[Trade]:
        book = self.engine.book
        if ev["k"] == "o":
            e = ev["e"]
            if e == "created":
                o = Order(BOOK, Side.BUY if ev["s"] == 0 else Side.SELL, self._qy(ev["a"]), price=self._px(ev["p"]))
                book._rest(o)                      # append to the FIFO at this price
                self._idmap[ev["id"]] = o
            elif e == "changed":
                o = self._idmap.get(ev["id"])
                if o is not None:
                    o.qty = max(1, self._qy(ev["a"]))
            elif e == "deleted":
                o = self._idmap.pop(ev["id"], None)
                if o is not None and ev.get("tr", 0) == 0:   # a true cancel; fills are handled by trades
                    book.cancel(o.order_id, BOOK)
            return []
        # a trade: consume the front of the resting side it hit (taker sell hits bids, buy hits asks)
        return self._consume_front(Side.BUY if ev["s"] == 1 else Side.SELL, self._px(ev["p"]), self._qy(ev["a"]))

    def _consume_front(self, side_hit: Side, price: int, size: int) -> list[Trade]:
        book = self.engine.book
        level = (book.bids if side_hit == Side.BUY else book.asks).get(price)
        fills: list[Trade] = []
        while size > 0 and level:
            maker = level[0]
            fill = min(size, maker.qty)
            maker.qty -= fill
            size -= fill
            if side_hit == Side.BUY:               # a resting bid is filled by an incoming sell
                buyer, seller, agg = maker.agent_id, BOOK, Side.SELL
            else:                                  # a resting ask is filled by an incoming buy
                buyer, seller, agg = BOOK, maker.agent_id, Side.BUY
            fills.append(Trade(tick=self._tick, price=price, qty=fill, buyer_id=buyer, seller_id=seller,
                               aggressor_side=agg, maker_order_id=maker.order_id, taker_order_id=0))
            if maker.qty == 0:
                level.popleft()
                book.live.pop(maker.order_id, None)
                book._pop_level_if_empty(side_hit, price)
        return fills

    def _apply_agent(self, order: Order) -> list[Trade]:
        """An agent order whose latency has elapsed reaches the matching engine now."""
        if order.order_type == OrderType.CANCEL:
            self.engine.book.cancel(order.cancel_id, order.agent_id)
            return []
        return self.engine.book.submit(order, self._tick)   # rests (joins FIFO) or crosses real liquidity

    def _drain_pending(self, now: int) -> list[Trade]:
        """Apply any agent orders whose land time has arrived, before the next real event at `now`."""
        if not self._pending:
            return []
        fills, still = [], []
        for land, order in self._pending:
            if land <= now:
                fills += self._apply_agent(order)
            else:
                still.append((land, order))
        self._pending = still
        return fills

    def _advance(self, n: int) -> list[Trade]:
        fills: list[Trade] = []
        end = min(self._cursor + n, len(self._events))
        for i in range(self._cursor, end):
            ev = self._events[i]
            fills += self._drain_pending(ev["t"])   # agent orders land at their latency-delayed time
            self._clock = ev["t"]
            fills += self._apply(ev)
        self._cursor = end
        if self._cursor >= len(self._events):
            fills += self._drain_pending(float("inf"))   # flush before wrapping (clock would reset)
            self._cursor = self._warmup
        return fills

    def _collect_orders(self, tick: int) -> list[Order]:
        # Don't apply agent orders immediately — stamp them with a landing time (decision + latency)
        # and let _advance interleave them with the real stream. Returning [] keeps process_tick a
        # no-op for these; the latency gap is exactly where the cancel race is won or lost.
        for o in super()._collect_orders(tick):
            self._pending.append((self._clock + self._latency_us, o))
        return []

    # -- Market hooks the tick loop calls --------------------------------
    def _seed_book(self):
        self._advance(self._warmup)               # build a realistic book before agents trade

    def _inject_fundamental(self, fv: float) -> None:
        self._tick += 1
        fills = self._advance(self._eptick)        # real orders rest/cancel; real trades fill the front
        if fills:
            self._settle(fills)                    # credit any agent orders that were at the front
            self.engine.trades.extend(fills)
            self.engine.last_price = fills[-1].price
        bb, ba = self.engine.book.best_bid(), self.engine.book.best_ask()
        if bb and ba:
            self.fundamental.value = (bb + ba) / 2
