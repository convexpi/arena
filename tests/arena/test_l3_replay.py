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
