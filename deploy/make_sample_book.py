#!/usr/bin/env python3
"""
make_sample_book.py — Generate a deterministic, realistic *synthetic* L2 order book for demos/CI.

This produces the same JSONL format as fetch_crypto_orderbook.py so book-replay mode runs
out-of-the-box without a network recording. It is clearly synthetic (a seeded random walk with a
plausible depth profile), so there is no exchange-data redistribution concern. For real depth, use
deploy/fetch_crypto_orderbook.py.

    python deploy/make_sample_book.py                 # -> data/sample_btcusdt_book.jsonl
    python deploy/make_sample_book.py --frames 480 --seed 7
"""

import argparse
import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from convexpi.arena.crypto_book_replay import save_jsonl  # noqa: E402


def make_frames(n: int, *, start: float, seed: int, levels: int, tick: float) -> list[dict]:
    rng = random.Random(seed)
    mid = start
    t0 = 1_700_000_000_000
    frames = []
    for i in range(n):
        # Mid: small Gaussian step + rare jump (a teaching-friendly random walk).
        mid *= math.exp(rng.gauss(0, 0.0006) + (rng.choice([-1, 1]) * 0.004 if rng.random() < 0.01 else 0))
        half = tick * (1 + rng.random())                      # half-spread, 1–2 ticks
        best_bid = round(mid - half, 2)
        best_ask = round(mid + half, 2)

        def side(best: float, direction: int):
            out = []
            for lvl in range(levels):
                price = round(best + direction * lvl * tick, 2)
                # Size grows a bit deeper into the book, with noise; in base units (e.g. BTC).
                size = round(max(0.01, (0.4 + 0.25 * lvl) * (0.5 + rng.random())), 3)
                out.append([price, size])
            return out

        frames.append({
            "t": t0 + i * 1000,
            "b": side(best_bid, -1),
            "a": side(best_ask, +1),
        })
    return frames


def main():
    p = argparse.ArgumentParser(description="Generate a synthetic sample order book (JSONL)")
    p.add_argument("--frames", type=int, default=300)
    p.add_argument("--start", type=float, default=60_000.0, help="Starting mid price")
    p.add_argument("--levels", type=int, default=15, help="Levels per side")
    p.add_argument("--tick", type=float, default=1.0, help="Price increment between levels")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", default="data/sample_btcusdt_book.jsonl")
    args = p.parse_args()

    frames = make_frames(args.frames, start=args.start, seed=args.seed,
                         levels=args.levels, tick=args.tick)
    save_jsonl(frames, args.out)
    mids = [(f["a"][0][0] + f["b"][0][0]) / 2 for f in frames]
    print(f"Wrote {len(frames)} synthetic snapshots → {args.out}")
    print(f"  mid {mids[0]:,.2f} → {mids[-1]:,.2f}  ({args.levels} levels/side)")


if __name__ == "__main__":
    main()
