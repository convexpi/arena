#!/usr/bin/env python3
"""
fetch_crypto_l3.py — Record real **L3 (order-by-order)** crypto data for the realistic exchange.

Unlike fetch_crypto_orderbook.py (L2 — aggregated size per price level, snapshotted), this captures
the *message stream of individual orders* from Bitstamp's public `live_orders` channel: every order's
create / change / delete, each with its own id, price, side, and microsecond timestamp — plus the
`live_trades` channel for executions. That's exactly what you need to reconstruct **queue position**
and model latency / cancel races.

The recording is SEEDED from a REST order-book snapshot first, so the book starts complete —
including orders that were already resting before we connected. Without this seed, replay begins
from an empty book and every change/delete/trade referencing a pre-existing order is orphaned,
which leaves the reconstructed book permanently crossed (stale best bid > best ask). The snapshot
(Bitstamp `order_book?group=2`, which carries per-order ids that match the `live_orders` stream) is
written as a leading block of synthetic `created` events sharing the snapshot timestamp; the replay
warms up through that whole block before agents trade.

Output JSONL (one event per line):
    {"k":"o","e":"created|changed|deleted","id":..,"p":<price>,"a":<remaining>,"s":0|1,"tr":<traded>,"t":<microts>}
    {"k":"t","p":<price>,"a":<size>,"s":0|1,"t":<microts>}     # a trade (s = taker side: 0 buy, 1 sell)
    (the leading run of `created` events all sharing the first timestamp is the seed snapshot)

Usage:
    python deploy/fetch_crypto_l3.py --pair btcusd --seconds 120 --out data/btcusd_l3.jsonl
"""
import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

try:
    import websockets
except ImportError:
    raise SystemExit("pip install websockets")

WS = "wss://ws.bitstamp.net"
REST = "https://www.bitstamp.net/api/v2/order_book/{pair}/?group=2"   # group=2 ⇒ per-order ids


def _fetch_snapshot(pair: str) -> dict:
    req = urllib.request.Request(REST.format(pair=pair),
                                 headers={"User-Agent": "convexpi-arena-recorder"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def _snapshot_events(snap: dict, depth: int) -> tuple[list[dict], int]:
    """Turn a REST order-book snapshot into leading `created` events (bids s=0, asks s=1), all
    stamped with the snapshot microtimestamp. `depth` caps orders per side (0 = full book)."""
    t0 = int(snap.get("microtimestamp") or (int(snap.get("timestamp", "0")) * 1_000_000))
    events: list[dict] = []
    for side, key in ((0, "bids"), (1, "asks")):
        levels = snap.get(key, [])
        if depth > 0:
            levels = levels[:depth]
        for entry in levels:
            if len(entry) < 3 or not entry[2]:
                continue                          # need the order id to match the live stream
            events.append({"k": "o", "e": "created", "id": int(entry[2]),
                           "p": float(entry[0]), "a": float(entry[1]),
                           "s": side, "tr": 0.0, "t": t0})
    return events, t0


async def record(pair: str, seconds: float, out: str, depth: int) -> int:
    sub = lambda ch: json.dumps({"event": "bts:subscribe", "data": {"channel": f"{ch}_{pair}"}})
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    n = 0
    deadline = time.monotonic() + seconds
    async with websockets.connect(WS, max_size=2**22) as ws:
      with open(out, "w") as f:
        # Subscribe first so the stream is buffering, then snapshot. Frames received during the
        # snapshot fetch stay readable afterwards; we drop any with t <= snapshot time (the
        # snapshot already reflects them) and keep the rest, the standard order-book sync.
        await ws.send(sub("live_orders"))
        await ws.send(sub("live_trades"))
        t0 = 0
        try:
            snap = await asyncio.get_event_loop().run_in_executor(None, _fetch_snapshot, pair)
            seed, t0 = _snapshot_events(snap, depth)
            for rec in seed:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            n += len(seed)
            print(f"  seeded {len(seed)} resting orders from REST snapshot @ {t0}")
        except Exception as e:
            print(f"  [snapshot seed failed: {type(e).__name__}: {e} — recording WITHOUT seed]")
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.monotonic())))
            except asyncio.TimeoutError:
                break
            ev, d = msg.get("event", ""), msg.get("data", {})
            if ev.startswith("order_"):
                rec = {"k": "o", "e": ev.split("_", 1)[1], "id": d["id"],
                       "p": float(d["price"]), "a": float(d["amount"]), "s": int(d["order_type"]),
                       "tr": float(d.get("amount_traded", 0) or 0), "t": int(d["microtimestamp"])}
            elif ev == "trade":
                rec = {"k": "t", "p": float(d["price"]), "a": float(d["amount"]),
                       "s": int(d["type"]), "t": int(d["microtimestamp"])}
            else:
                continue
            if rec["t"] <= t0:           # already captured by the snapshot — skip to avoid stale dupes
                continue
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            n += 1
            if n % 500 == 0:
                print(f"  {n} events…")
    return n


def main():
    p = argparse.ArgumentParser(description="Record real L3 crypto order-flow (Bitstamp)")
    p.add_argument("--pair", default="btcusd")
    p.add_argument("--seconds", type=float, default=120)
    p.add_argument("--depth", type=int, default=1200,
                   help="Max resting orders per side to seed from the REST snapshot (0 = full book)")
    p.add_argument("--out", default=None)
    args = p.parse_args()
    out = args.out or f"data/{args.pair}_l3.jsonl"
    print(f"Recording {args.pair} L3 order-flow for {args.seconds:.0f}s → {out}")
    n = asyncio.run(record(args.pair, args.seconds, out, args.depth))
    print(f"Wrote {n} events → {out}")
    if not n:
        sys.exit(1)


if __name__ == "__main__":
    main()
