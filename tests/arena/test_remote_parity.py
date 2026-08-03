"""Remote competitors and background agents must enter the market on identical terms.

Two ways they previously diverged:
  - size: `qty` meant lots for one and engine units for the other (fixed by lot_size)
  - timing: remote orders went straight to the engine while background orders paid the L3
    order-entry latency, so a competitor's order landed instantly and never ran the cancel race
    that is the entire point of that venue
"""
import json

import pytest

from convexpi.arena.crypto_book_replay import CryptoBookFeed, CryptoBookReplayMarket
from convexpi.arena.crypto_l3_replay import MboReplayMarket
from convexpi.arena.engine import Order, OrderType, Side
from convexpi.arena.market import Market
from convexpi.arena.server import _default_background, _state_to_json

L3_PATH = "data/btcusd_l3_sample.jsonl"


def _l3(**kw):
    return MboReplayMarket(_default_background(), l3_path=L3_PATH,
                           n_ticks=kw.pop("n_ticks", 5), seed=1, **kw)


# ---------------------------------------------------------------------------
# Timing parity
# ---------------------------------------------------------------------------

def test_l3_defers_remote_orders_into_the_latency_queue():
    """The regression: remote orders must not reach the engine this tick."""
    m = _l3(latency_us=5_000)
    pending_before = len(m._pending)
    passed_through = m.submit_external([Order("remote", Side.BUY, 1, price=100)], tick=1)
    assert passed_through == [], "remote order went straight to the engine, skipping latency"
    assert len(m._pending) == pending_before + 1, "remote order never entered the latency queue"


def test_remote_and_background_orders_get_the_same_landing_time():
    """Same submission tick, same latency — neither population may be advantaged."""
    m = _l3(latency_us=5_000)
    m._pending.clear()
    m.submit_external([Order("remote", Side.BUY, 1, price=100)], tick=1)
    m._defer(m.to_engine_qty([Order("background", Side.BUY, 1, price=100)]))
    assert len({land for land, _ in m._pending}) == 1, "landing times differ between populations"


def test_remote_orders_are_lot_converted_on_the_deferred_path():
    """Deferring must not skip the size conversion."""
    m = _l3()
    m._pending.clear()
    m.submit_external([Order("remote", Side.BUY, 3, price=100)], tick=1)
    assert [o.qty for _, o in m._pending] == [3 * m.lot_size]


def test_venues_without_latency_pass_remote_orders_straight_through():
    """The deferral is L3's mechanic, not a global one — a plain venue must not swallow orders."""
    m = Market(_default_background(), n_ticks=5)
    out = m.submit_external([Order("remote", Side.BUY, 4, price=100)], tick=1)
    assert [o.qty for o in out] == [4]

    feed = CryptoBookFeed("data/btcusd_book.jsonl", cents_per_unit=100, qty_scale=1000, loop=True)
    book = CryptoBookReplayMarket(_default_background(), feed=feed, n_ticks=5, seed=1)
    assert [o.qty for o in book.submit_external([Order("r", Side.BUY, 4, price=100)], 1)] == [4]


# ---------------------------------------------------------------------------
# Unit legibility
# ---------------------------------------------------------------------------

def test_state_publishes_the_lot_conversion():
    m = _l3()
    st = m.build_state("noise_0", tick=1)
    assert st.lot_size == m.lot_size == 1000


def test_position_lots_reads_against_engine_units():
    m = _l3()
    m.accounts["noise_0"].position = 2500          # 2.5 lots after a partial fill
    st = m.build_state("noise_0", tick=1)
    assert st.position == 2500
    assert st.position_lots == pytest.approx(2.5), "partial fills must not round away"


def test_share_market_reports_lots_and_units_identically():
    m = Market(_default_background(), n_ticks=5)
    m.accounts["noise_0"].position = 7
    st = m.build_state("noise_0", tick=1)
    assert (st.lot_size, st.position, st.position_lots) == (1, 7, 7)


def test_wire_state_carries_lot_size_and_lots():
    """Remote agents can only convert if the factor is actually on the wire."""
    m = _l3()
    m.accounts["noise_0"].position = 3000
    payload = json.loads(_state_to_json(m.build_state("noise_0", tick=1)))
    assert payload["lot_size"] == 1000
    assert payload["position"] == 3000
    assert payload["position_lots"] == pytest.approx(3.0)
