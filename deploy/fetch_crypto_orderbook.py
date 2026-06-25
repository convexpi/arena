#!/usr/bin/env python3
"""
fetch_crypto_orderbook.py — Record real L2 order-book snapshots for Arena book-replay mode.

Polls a public depth endpoint (Binance or Coinbase — no API key) every `--interval` seconds and
writes one JSONL snapshot per poll. Unlike fetch_crypto_data.py (which records OHLCV *price* bars),
this records the actual **bids and asks**, so the Arena can replay a real order book that students
trade against — real depth, real slippage.

Usage:
    # Record 300 snapshots of BTC/USDT depth, ~1s apart (~5 min) -> data/btcusdt_book.jsonl
    python deploy/fetch_crypto_orderbook.py
    python deploy/fetch_crypto_orderbook.py --symbol ETHUSDT --frames 600 --interval 0.5
    python deploy/fetch_crypto_orderbook.py --source coinbase --symbol BTC-USD --levels 25

Then run the Arena in book-replay mode:
    python -m convexpi.arena.server --crypto-book data/btcusdt_book.jsonl
"""

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from convexpi.arena.crypto_book_replay import save_jsonl  # noqa: E402

UA = {"User-Agent": "convexpi/1.0"}


def _get(url: str) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
        return json.loads(r.read())


def fetch_binance(symbol: str, levels: int) -> dict:
    # Binance allows limit in {5,10,20,50,100,500,1000}; round up to a valid tier.
    tier = next(t for t in (5, 10, 20, 50, 100, 500, 1000) if t >= levels)
    d = _get(f"https://api.binance.com/api/v3/depth?symbol={symbol.upper()}&limit={tier}")
    b = [[float(p), float(q)] for p, q in d["bids"][:levels]]
    a = [[float(p), float(q)] for p, q in d["asks"][:levels]]
    return {"t": int(time.time() * 1000), "b": b, "a": a}


def fetch_coinbase(product: str, levels: int) -> dict:
    # Coinbase Exchange public level-2 book (top 50 each side).
    d = _get(f"https://api.exchange.coinbase.com/products/{product}/book?level=2")
    b = [[float(p), float(q)] for p, q, *_ in d["bids"][:levels]]
    a = [[float(p), float(q)] for p, q, *_ in d["asks"][:levels]]
    return {"t": int(time.time() * 1000), "b": b, "a": a}


def main():
    p = argparse.ArgumentParser(description="Record real crypto L2 order-book snapshots for Arena replay")
    p.add_argument("--symbol", default="BTCUSDT", help="Binance: BTCUSDT; Coinbase: BTC-USD")
    p.add_argument("--source", choices=["binance", "coinbase"], default="binance")
    p.add_argument("--frames", type=int, default=300, help="Number of snapshots to record")
    p.add_argument("--interval", type=float, default=1.0, help="Seconds between polls")
    p.add_argument("--levels", type=int, default=20, help="Book levels per side to keep")
    p.add_argument("--out", default=None, help="Output JSONL (default: data/<symbol>_book.jsonl)")
    args = p.parse_args()

    out = args.out or f"data/{args.symbol.lower().replace('-', '')}_book.jsonl"
    fetch = (lambda: fetch_binance(args.symbol, args.levels)) if args.source == "binance" \
        else (lambda: fetch_coinbase(args.symbol, args.levels))

    print(f"Recording {args.frames} snapshots of {args.symbol} from {args.source} "
          f"every {args.interval}s (~{args.frames * args.interval / 60:.1f} min)…")
    frames, errors = [], 0
    for i in range(args.frames):
        try:
            fr = fetch()
            frames.append(fr)
            if (i + 1) % 25 == 0 or i == 0:
                spread = fr["a"][0][0] - fr["b"][0][0] if fr["a"] and fr["b"] else float("nan")
                print(f"  [{i + 1:>4}/{args.frames}] mid≈{(fr['a'][0][0] + fr['b'][0][0]) / 2:,.2f} "
                      f"spread={spread:.2f}")
        except Exception as e:                       # noqa: BLE001 — keep recording through blips
            errors += 1
            print(f"  [{i + 1:>4}] fetch error: {e}", file=sys.stderr)
        if i < args.frames - 1:
            time.sleep(args.interval)

    if not frames:
        print("ERROR: no snapshots recorded", file=sys.stderr)
        sys.exit(1)

    save_jsonl(frames, out)
    print(f"Wrote {len(frames)} snapshots ({errors} errors) → {out}")


if __name__ == "__main__":
    main()
