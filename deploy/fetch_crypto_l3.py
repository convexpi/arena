#!/usr/bin/env python3
"""
fetch_crypto_l3.py — Record real **L3 (order-by-order)** crypto data for the realistic exchange.

Unlike fetch_crypto_orderbook.py (L2 — aggregated size per price level, snapshotted), this captures
the *message stream of individual orders* from Bitstamp's public `live_orders` channel: every order's
create / change / delete, each with its own id, price, side, and microsecond timestamp — plus the
`live_trades` channel for executions. That's exactly what you need to reconstruct **queue position**
and model latency / cancel races.

Output JSONL (one event per line):
    {"k":"o","e":"created|changed|deleted","id":..,"p":<price>,"a":<remaining>,"s":0|1,"tr":<traded>,"t":<microts>}
    {"k":"t","p":<price>,"a":<size>,"s":0|1,"t":<microts>}     # a trade (s = taker side: 0 buy, 1 sell)

Usage:
    python deploy/fetch_crypto_l3.py --pair btcusd --seconds 120 --out data/btcusd_l3.jsonl
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

try:
    import websockets
except ImportError:
    raise SystemExit("pip install websockets")

WS = "wss://ws.bitstamp.net"


async def record(pair: str, seconds: float, out: str) -> int:
    sub = lambda ch: json.dumps({"event": "bts:subscribe", "data": {"channel": f"{ch}_{pair}"}})
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    n = 0
    deadline = time.monotonic() + seconds
    async with websockets.connect(WS, max_size=2**22) as ws:
      with open(out, "w") as f:
        await ws.send(sub("live_orders"))
        await ws.send(sub("live_trades"))
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
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
            n += 1
            if n % 500 == 0:
                print(f"  {n} events…")
    return n


def main():
    p = argparse.ArgumentParser(description="Record real L3 crypto order-flow (Bitstamp)")
    p.add_argument("--pair", default="btcusd")
    p.add_argument("--seconds", type=float, default=120)
    p.add_argument("--out", default=None)
    args = p.parse_args()
    out = args.out or f"data/{args.pair}_l3.jsonl"
    print(f"Recording {args.pair} L3 order-flow for {args.seconds:.0f}s → {out}")
    n = asyncio.run(record(args.pair, args.seconds, out))
    print(f"Wrote {n} events → {out}")
    if not n:
        sys.exit(1)


if __name__ == "__main__":
    main()
