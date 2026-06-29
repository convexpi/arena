"""MboReplayMarket: agents get real FIFO queue position and queue-based fills from the L3 stream."""

import json

from convexpi.arena.crypto_l3_replay import MboReplayMarket
from convexpi.arena.agents import Agent
from convexpi.arena.engine import Side


class PassiveBuyer(Agent):
    """Post a single resting buy at a fixed price (cents) on the first tick, then hold."""
    def __init__(self, agent_id, price_cents, qty):
        super().__init__(agent_id)
        self.price_cents = price_cents
        self.qty = qty
        self._done = False

    def on_tick(self, state):
        if self._done:
            return []
        self._done = True
        return [self.limit(Side.BUY, self.price_cents, self.qty)]


def _write(tmp_path, events):
    p = tmp_path / "l3.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in events))
    return str(p)


# 100.0 -> 10000 cents; 0.02 BTC -> 20000 units (qty_scale 1e6)
def test_agent_fills_when_at_front(tmp_path):
    events = [
        {"k": "o", "e": "created", "id": 1, "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 1_000_000},  # warmup real bid
        {"k": "o", "e": "deleted", "id": 1, "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 2_000_000},  # tick1: real bid cancels
        {"k": "t", "p": 100.0, "a": 0.02, "s": 1, "t": 3_000_000},                                    # tick2: sell hits the bid
    ]
    agent = PassiveBuyer("alice", 10000, 20000)
    m = MboReplayMarket([agent], l3_path=_write(tmp_path, events),
                        warmup_events=1, events_per_tick=1, n_ticks=2)
    m.run()
    # Real order ahead cancelled, then a trade reached the agent -> it filled.
    assert m.accounts["alice"].position == 20000


def test_queue_position_protects_agent(tmp_path):
    events = [
        {"k": "o", "e": "created", "id": 1, "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 1_000_000},  # warmup real bid (ahead)
        {"k": "o", "e": "created", "id": 9, "p": 90.0, "a": 0.1, "s": 0, "tr": 0, "t": 2_000_000},    # tick1: filler far away
        {"k": "t", "p": 100.0, "a": 0.02, "s": 1, "t": 3_000_000},                                    # tick2: small sell hits the bid
    ]
    agent = PassiveBuyer("bob", 10000, 20000)
    m = MboReplayMarket([agent], l3_path=_write(tmp_path, events),
                        warmup_events=1, events_per_tick=1, n_ticks=2)
    m.run()
    # The 0.02 trade is consumed by the 0.5 real order ahead of us in the queue -> we are NOT filled.
    assert m.accounts["bob"].position == 0


from convexpi.arena.engine import Order, OrderType


class PostThenCancel(Agent):
    """Post a resting buy on tick 1, then try to cancel it on tick 3 (reacting to 'toxic' flow)."""
    def __init__(self, agent_id, price_cents, qty):
        super().__init__(agent_id)
        self.price_cents, self.qty = price_cents, qty
        self.n, self.oid = 0, None

    def on_tick(self, state):
        self.n += 1
        if self.n == 1:
            o = self.limit(Side.BUY, self.price_cents, self.qty)
            self.oid = o.order_id
            return [o]
        if self.n == 3 and self.oid is not None:
            return [Order(self.agent_id, Side.BUY, 1, order_type=OrderType.CANCEL, cancel_id=self.oid)]
        return []


def _race_events():
    return [
        {"k": "o", "e": "created", "id": 1, "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 1_000_000},  # warmup bid
        {"k": "o", "e": "deleted", "id": 1, "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 2_000_000},  # t1: bid cancels
        {"k": "o", "e": "created", "id": 9, "p": 90.0, "a": 0.1, "s": 0, "tr": 0, "t": 5_000_000},   # t2: filler
        {"k": "o", "e": "created", "id": 8, "p": 89.0, "a": 0.1, "s": 0, "tr": 0, "t": 8_000_000},   # t3: filler (agent cancels here)
        {"k": "t", "p": 100.0, "a": 0.02, "s": 1, "t": 11_000_000},                                  # t4: sell hits our price
    ]


def test_fast_cancel_beats_the_trade(tmp_path):
    agent = PostThenCancel("fast", 10000, 20000)
    m = MboReplayMarket([agent], l3_path=_write(tmp_path, _race_events()),
                        warmup_events=1, events_per_tick=1, n_ticks=4, latency_us=100_000)
    m.run()
    assert m.accounts["fast"].position == 0          # cancel landed before the trade


def test_slow_cancel_gets_adversely_filled(tmp_path):
    agent = PostThenCancel("slow", 10000, 20000)
    m = MboReplayMarket([agent], l3_path=_write(tmp_path, _race_events()),
                        warmup_events=1, events_per_tick=1, n_ticks=4, latency_us=4_000_000)
    m.run()
    assert m.accounts["slow"].position == 20000      # trade filled us before the slow cancel landed


from convexpi.arena.crypto_l3_replay import BOOK
from convexpi.arena.agents import NoiseTrader


def test_fill_deleted_order_is_swept(tmp_path):
    # A real order deleted by a trade (tr != 0) is popped from the id map but skipped on the book,
    # relying on a trade event to consume it. With no matching trade, it strands; the orphan sweep
    # must remove it so dead liquidity doesn't accumulate and cross the book.
    events = [
        {"k": "o", "e": "created", "id": 1, "p": 100.0, "a": 0.5, "s": 0, "tr": 0,   "t": 1_000_000},
        {"k": "o", "e": "deleted", "id": 1, "p": 100.0, "a": 0.0, "s": 0, "tr": 0.5, "t": 2_000_000},
    ]
    m = MboReplayMarket([PassiveBuyer("a", 1, 1)], l3_path=_write(tmp_path, events),
                        warmup_events=1, events_per_tick=1, n_ticks=1)
    m.run()
    assert all(o.agent_id != BOOK for o in m.engine.book.live.values())   # no stranded real liquidity


def test_snapshot_seed_builds_complete_uncrossed_book(tmp_path):
    # A snapshot-seeded recording opens with a block of `created` events sharing one timestamp.
    # The replay must warm up through the whole block so the real book starts complete & uncrossed.
    T0 = 1_000_000
    snap = (
        [{"k": "o", "e": "created", "id": 1000 + i, "p": p, "a": 0.5, "s": 0, "tr": 0, "t": T0}
         for i, p in enumerate([100.0, 99.0, 98.0])]        # bids
        + [{"k": "o", "e": "created", "id": 2000 + i, "p": p, "a": 0.5, "s": 1, "tr": 0, "t": T0}
           for i, p in enumerate([101.0, 102.0, 103.0])]    # asks
    )
    stream = [{"k": "o", "e": "created", "id": 3001, "p": 100.5, "a": 0.2, "s": 0, "tr": 0, "t": T0 + 1000}]
    m = MboReplayMarket([NoiseTrader("n", seed=1)], l3_path=_write(tmp_path, snap + stream),
                        warmup_events=1, events_per_tick=1, n_ticks=1)
    assert m._warmup == len(snap)                            # warmed up through the whole snapshot
    m._seed_book()
    book = m.engine.book
    assert max(book.bids) < min(book.asks)                   # raw book is complete and uncrossed
    assert len(book.live) == len(snap)


def test_marketable_created_is_not_rested(tmp_path):
    # Bitstamp emits `created` for marketable orders too. Such a taker must NOT rest crossed (that
    # stranded deep stale liquidity); its fills come from the trade stream. A `created` whose price
    # crosses the live book is dropped from the resting book, which stays uncrossed.
    events = [
        {"k": "o", "e": "created", "id": 1, "p":  98.0, "a": 0.5, "s": 0, "tr": 0, "t": 1_000_000},  # bid 98 rests
        {"k": "o", "e": "created", "id": 2, "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 1_100_000},  # bid 100 rests
        {"k": "o", "e": "created", "id": 3, "p":  99.0, "a": 0.5, "s": 1, "tr": 0, "t": 1_200_000},  # ask 99 crosses -> dropped
    ]
    m = MboReplayMarket([NoiseTrader("n", seed=1)], l3_path=_write(tmp_path, events),
                        warmup_events=3, events_per_tick=1, n_ticks=1)
    m._seed_book()
    book = m.engine.book
    assert not book.asks                                    # the marketable ask was not rested
    assert set(book.bids) == {9800, 10000}                  # resting bids intact, book uncrossed
