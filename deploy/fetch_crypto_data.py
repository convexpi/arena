#!/usr/bin/env python3
"""
fetch_crypto_data.py — Download OHLCV data for crypto replay mode.

Fetches up to 1000 1-minute bars from the Binance public API (no API key
required) and saves them to a CSV file that the Arena server can replay.

Usage:
    python deploy/fetch_crypto_data.py                      # BTC/USDT, 1000 bars → data/btcusdt.csv
    python deploy/fetch_crypto_data.py --symbol ETHUSDT     # ETH/USDT
    python deploy/fetch_crypto_data.py --limit 500 --out data/btc_short.csv
    python deploy/fetch_crypto_data.py --source coinbase --symbol BTC-USD

Then run the Arena server in replay mode:
    python -m convexpi.arena.server --crypto-data data/btcusdt.csv
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from convexpi.arena.crypto_replay import (
    load_binance_klines,
    load_coinbase_candles,
    _save_csv,
)


def main():
    p = argparse.ArgumentParser(description="Fetch crypto OHLCV data for Arena replay")
    p.add_argument("--symbol",  default="BTCUSDT",
                   help="Symbol to fetch (Binance: BTCUSDT; Coinbase: BTC-USD)")
    p.add_argument("--limit",   type=int, default=1000,
                   help="Number of 1-minute bars (max 1000 for Binance, 300 for Coinbase)")
    p.add_argument("--out",     default=None,
                   help="Output CSV path (default: data/<symbol_lower>.csv)")
    p.add_argument("--source",  choices=["binance", "coinbase"], default="binance")
    args = p.parse_args()

    symbol = args.symbol
    out = args.out or f"data/{symbol.lower().replace('-', '')}.csv"

    print(f"Fetching {args.limit} bars for {symbol} from {args.source}…")

    if args.source == "binance":
        bars = load_binance_klines(symbol, limit=args.limit, out_path=out)
    else:
        bars = load_coinbase_candles(symbol, limit=args.limit, out_path=out)

    if not bars:
        print("ERROR: no bars returned", file=sys.stderr)
        sys.exit(1)

    from datetime import datetime, timezone
    t0 = datetime.fromtimestamp(bars[0].timestamp_ms / 1000, tz=timezone.utc)
    t1 = datetime.fromtimestamp(bars[-1].timestamp_ms / 1000, tz=timezone.utc)

    print(f"  {len(bars)} bars  {t0:%Y-%m-%d %H:%M} UTC → {t1:%Y-%m-%d %H:%M} UTC")
    print(f"  price range: ${bars[0].close:.2f} → ${bars[-1].close:.2f}")
    print(f"  saved to: {out}")
    print()
    print(f"Run the Arena server in crypto replay mode:")
    print(f"  python -m convexpi.arena.server --crypto-data {out}")


if __name__ == "__main__":
    main()
