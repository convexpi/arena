"""Tests for the reference market-by-order (L3) engine: queue position, fills, cancel race."""

from convexpi.arena.mbo import L3Book, simulate_passive_order


def test_book_reconstruction_and_touch():
    b = L3Book()
    b.apply({"e": "created", "id": 1, "p": 100.0, "a": 0.5, "s": 0})   # bid
    b.apply({"e": "created", "id": 2, "p": 101.0, "a": 0.3, "s": 1})   # ask
    b.apply({"e": "created", "id": 3, "p": 99.0, "a": 1.0, "s": 0})    # lower bid
    assert b.best_bid() == 100.0 and b.best_ask() == 101.0
    assert b.size_at(0, 100.0) == 0.5
    b.apply({"e": "deleted", "id": 1, "p": 100.0, "a": 0.5, "s": 0})
    assert b.best_bid() == 99.0                                        # level emptied -> next bid


def _scenario():
    # A 0.5 BTC bid rests ahead of us at 100; we join at t=2s; A cancels at 3s; a 0.02 sell
    # trades at 100 at 4s (which should reach and fill us once A is gone).
    return [
        {"k": "o", "e": "created", "id": 1,  "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 1_000_000},
        {"k": "o", "e": "created", "id": 50, "p": 105.0, "a": 0.1, "s": 1, "tr": 0, "t": 2_000_000},  # enter here
        {"k": "o", "e": "deleted", "id": 1,  "p": 100.0, "a": 0.5, "s": 0, "tr": 0, "t": 3_000_000},
        {"k": "t", "p": 100.0, "a": 0.02, "s": 1, "t": 4_000_000},   # taker sell hits the bid
    ]


def test_queue_drains_and_fills():
    ev = _scenario()
    r = simulate_passive_order(ev, side=0, price=100.0, enter_idx=1, size=0.02)
    assert r.initial_queue_ahead == 0.5
    assert r.filled and not r.cancelled
    assert r.fill_ts == 4_000_000
    assert abs(r.time_to_fill_s - 2.0) < 1e-9


def test_cancel_beats_fill():
    ev = _scenario()
    # decide to cancel 0.5s after entering; cancel lands fast (0.1s) -> before the 4s trade
    r = simulate_passive_order(ev, side=0, price=100.0, enter_idx=1, size=0.02,
                               cancel_after_s=0.5, latency_us=100_000)
    assert r.cancelled and not r.filled


def test_slow_cancel_gets_adversely_filled():
    ev = _scenario()
    # same decision, but the cancel takes 3s to land -> the 4s fill happens first
    r = simulate_passive_order(ev, side=0, price=100.0, enter_idx=1, size=0.02,
                               cancel_after_s=0.5, latency_us=3_000_000)
    assert r.filled and not r.cancelled
