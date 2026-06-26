"""Conformance: the Rust L3 core must match the Python reference field-for-field.

Skipped automatically when `convexpi_arena_rs` isn't installed. Build it with:
    cd arena/rust && maturin develop --release
"""
import math
import os
import time

import pytest

from convexpi.arena import mbo

pytestmark = pytest.mark.skipif(not mbo.HAS_RUST, reason="convexpi_arena_rs not installed")

rs = mbo._rs
py = mbo._simulate_passive_order_py

_RESULT_FIELDS = [
    "side", "price", "size", "enter_ts", "initial_queue_ahead",
    "filled", "cancelled", "fill_ts", "reached_front_ts", "adverse",
    "time_to_fill_s",
]


def _assert_same_result(a, b):
    for f in _RESULT_FIELDS:
        va, vb = getattr(a, f), getattr(b, f)
        if isinstance(va, float) or isinstance(vb, float):
            assert va is not None and vb is not None
            assert math.isclose(va, vb, rel_tol=1e-12, abs_tol=1e-9), f
        else:
            assert va == vb, f
    assert len(a.queue_trace) == len(b.queue_trace), "queue_trace length"
    for (ta, qa), (tb, qb) in zip(a.queue_trace, b.queue_trace):
        assert ta == tb
        assert math.isclose(qa, qb, rel_tol=1e-12, abs_tol=1e-9)


# --------------------------------------------------------------------------
# constructed scenarios
# --------------------------------------------------------------------------

def _build(price=60000.0):
    ev, t = [], 0
    for oid in (1, 2, 3):
        ev.append({"k": "o", "e": "created", "id": oid, "p": price, "a": 1.0, "s": 0, "t": t}); t += 1_000_000
    enter = len(ev)
    ev.append({"k": "o", "e": "created", "id": 9, "p": price - 1, "a": 5.0, "s": 0, "t": t}); t += 1_000_000
    for _ in range(5):
        ev.append({"k": "t", "p": price, "a": 1.0, "s": 1, "t": t}); t += 2_000_000
    return ev, enter


@pytest.mark.parametrize("kw", [
    {},
    {"cancel_after_s": 1.0, "latency_us": 100_000},
    {"cancel_after_s": 7.0, "latency_us": 5_000_000},
    {"cancel_after_s": 0.0, "latency_us": 0},
])
def test_constructed_parity(kw):
    ev, enter = _build()
    _assert_same_result(
        py(ev, 0, 60000.0, enter, 0.5, **kw),
        rs.simulate_passive_order(ev, 0, 60000.0, enter, 0.5, **kw),
    )


def test_partial_then_full_fill_parity():
    ev, enter = _build()
    for size in (0.5, 1.0, 2.5, 4.0, 10.0):   # 10.0 never fully fills in the window
        _assert_same_result(
            py(ev, 0, 60000.0, enter, size),
            rs.simulate_passive_order(ev, 0, 60000.0, enter, size),
        )


# --------------------------------------------------------------------------
# real Bitstamp L3 data
# --------------------------------------------------------------------------

_DATA = os.path.join(os.path.dirname(__file__), "..", "..", "data", "btcusd_l3_sample.jsonl")


@pytest.fixture(scope="module")
def events():
    if not os.path.exists(_DATA):
        pytest.skip("sample L3 data not present")
    return mbo.load_l3(_DATA)


def _sample_entries(events, every=150, start=800, n=40):
    """Walk the book; yield (idx, best_bid) at clean (uncrossed) touches."""
    book = mbo.L3Book()
    out = []
    for i, e in enumerate(events):
        if e["k"] == "o":
            book.apply(e)
        if i > start and i % every == 0:
            bb, ba = book.best_bid(), book.best_ask()
            if bb is not None and ba is not None and bb < ba:
                out.append((i, bb))
        if len(out) >= n:
            break
    return out


def test_real_data_simulate_parity(events):
    entries = _sample_entries(events)
    assert entries, "no clean entry points found"
    for i, bb in entries:
        for kw in ({}, {"cancel_after_s": 0.2, "latency_us": 1_000_000}):
            _assert_same_result(
                py(events, 0, bb, i, 0.02, **kw),
                rs.simulate_passive_order(events, 0, bb, i, 0.02, **kw),
            )


def test_real_data_l3book_parity(events):
    """Reconstruct the book in both engines and compare the touch as it evolves."""
    pb, rb = mbo.L3Book(), rs.L3Book()
    for i, e in enumerate(events):
        if e["k"] == "o":
            pb.apply(e)
            rb.apply(e)
        if i % 200 == 0:
            assert pb.best_bid() == rb.best_bid()
            assert pb.best_ask() == rb.best_ask()
            assert pb.clean_touch() == rb.clean_touch()
            bb = pb.best_bid()
            if bb is not None:
                # size_at sums level amounts; float accumulation order may differ by ~1 ULP.
                assert math.isclose(pb.size_at(0, bb), rb.size_at(0, bb), rel_tol=1e-12, abs_tol=1e-9)
                assert pb.order_ids_at(0, bb) == rb.order_ids_at(0, bb)


# --------------------------------------------------------------------------
# benchmark (informational; asserts Rust is at least not slower on real data)
# --------------------------------------------------------------------------

def test_benchmark(events):
    entries = _sample_entries(events, n=20)
    reps = 5

    def run(fn):
        t0 = time.perf_counter()
        for _ in range(reps):
            for i, bb in entries:
                fn(events, 0, bb, i, 0.02)
        return time.perf_counter() - t0

    t_py = run(py)
    t_rs = run(rs.simulate_passive_order)
    speedup = t_py / t_rs if t_rs else float("inf")
    print(f"\n[L3 simulate] python={t_py*1e3:.1f}ms  rust={t_rs*1e3:.1f}ms  speedup={speedup:.1f}x")
    # On a real ~14k-event stream the Rust core should be clearly faster.
    assert t_rs < t_py
