#!/usr/bin/env python3
"""
mbo_demo.py — demonstrate the realistic (L3 / order-by-order) exchange model.

Runs the reference market-by-order engine (convexpi.arena.mbo) over a recorded L3 session and shows
the things the L2 snapshot model can't: your FIFO **queue position** and how it drains, and the
**cancel/latency race** that decides whether you dodge an adverse fill.

    python deploy/mbo_demo.py [data/btcusd_l3_sample.jsonl]

Note on reconstruction: a production replay seeds the book from an exchange L3 *snapshot* and then
applies the message stream. The free Bitstamp feed has no L3 snapshot, so we warm up on the stream
and use a cleaned (uncrossed) touch — fine for a teaching demonstration; the queue mechanics per price
level are exact.
"""
import sys

sys.path.insert(0, __file__.rsplit("/deploy/", 1)[0] + "/src")
from convexpi.arena.mbo import L3Book, simulate_passive_order, load_l3   # noqa: E402


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "data/btcusd_l3_sample.jsonl"
    ev = load_l3(path)
    print(f"Loaded {len(ev):,} L3 events from {path}")
    trades = sum(1 for e in ev if e["k"] == "t")
    span_s = (ev[-1]["t"] - ev[0]["t"]) / 1e6
    print(f"  {span_s:.0f}s span, {trades} trades, {len(ev) - trades:,} order events\n")

    # Warm up to ~40% through, find a clean resting level.
    idx = int(len(ev) * 0.4)
    book = L3Book()
    for e in ev[:idx]:
        if e["k"] == "o":
            book.apply(e)
    bid, ask = book.clean_touch()
    print(f"Book at warm-up: bid {bid}  ask {ask}  spread ${(ask - bid):.2f}")
    print(f"  size resting at best bid: {book.size_at(0, bid):.4f} BTC "
          f"({len(book.order_ids_at(0, bid))} individual orders ahead)\n")

    # 1) Passive buy joining the bid — watch the queue drain order by order.
    r = simulate_passive_order(ev, side=0, price=bid, enter_idx=idx, size=0.02)
    qs = [q for _, q in r.queue_trace]
    print("1) Passive buy 0.02 BTC at the bid — queue position over time:")
    print(f"   joined behind {r.initial_queue_ahead:.4f} BTC")
    if qs:
        print(f"   queue ahead drained: {qs[0]:.4f} → min {min(qs):.4f} BTC over {len(qs):,} events")
    print(f"   reached front of queue: {r.reached_front_ts is not None}")
    print(f"   filled: {r.filled}" + (f"  in {r.time_to_fill_s:.1f}s  (adverse={r.adverse})" if r.filled else
          "  (no trade reached our price in the window — passive orders often wait)"))

    # 2) The cancel/latency race — hold the quote, then pull it.
    print("\n2) The cancel race (hold 3s, then cancel; vary how long the cancel takes to land):")
    for lat_ms in (5, 100, 1000):
        rr = simulate_passive_order(ev, side=0, price=bid, enter_idx=idx, size=0.02,
                                    cancel_after_s=3.0, latency_us=lat_ms * 1000)
        outcome = "FILLED before cancel landed (adverse)" if rr.filled else "cancelled in time"
        print(f"   cancel latency {lat_ms:>4} ms → {outcome}")

    print("\nThis is the realistic-exchange model: real FIFO queues, real order-by-order drain, and a "
          "latency-sensitive cancel race — none of which the 1-second L2 snapshot can represent.")


if __name__ == "__main__":
    main()
