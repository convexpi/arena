# convexpi-arena-rs

Rust-accelerated **L3 (market-by-order)** core for
[`convexpi-arena`](https://github.com/convexpi/arena).

This crate is a faithful port of the reference Python semantics in
`convexpi/arena/mbo.py` — `L3Book` (an order-by-order book with FIFO queues per
price level) and `simulate_passive_order` (queue position → fill, the cancel
race, and adverse selection). The Python module remains the canonical definition
of the semantics; this crate exists to run the same logic at production-replay
speed.

Parity is enforced by `tests/arena/test_rust_conformance.py` in the parent repo,
which runs both implementations on constructed scenarios *and* a real Bitstamp
L3 capture and asserts they agree field-for-field.

## Build

```bash
# from arena/rust/
pip install maturin
maturin develop --release          # builds and installs `convexpi_arena_rs` into the active env
```

When the extension is importable, `convexpi.arena.mbo.simulate_passive_order`
transparently dispatches to it (see `mbo.HAS_RUST` / `mbo.USE_RUST`); otherwise
it falls back to the pure-Python reference.

## API

```python
import convexpi_arena_rs as rs

book = rs.L3Book()                 # .apply(ev), .best_bid(), .best_ask(),
                                   # .clean_touch(), .size_at(side, price),
                                   # .order_ids_at(side, price)

res = rs.simulate_passive_order(events, side, price, enter_idx, size,
                                cancel_after_s=None, latency_us=0)
res.filled, res.cancelled, res.time_to_fill_s, res.adverse, res.queue_trace
```

Event and result schemas match `convexpi.arena.mbo` exactly.
