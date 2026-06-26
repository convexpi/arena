# Changelog

All notable changes to `convexpi-arena` are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-06-26

The realistic-exchange release. The Arena gains a full order-by-order (L3) engine and
real-market replay modes, so strategies can be tested against genuine queue dynamics and
recorded market depth — not just synthetic background flow.

### Added
- **L3 (market-by-order) engine** (`convexpi.arena.mbo`): a reference Python implementation
  of an order-by-order book. `L3Book` reconstructs FIFO queues per price level;
  `simulate_passive_order(...)` rests a limit order and replays the stream order-by-order to
  model **queue position**, fills, the **cancel race**, and **adverse selection**
  (`cancel_after_s`, `latency_us`). `load_l3(path)` reads an L3 event stream.
- **Live L3 arena instance** (`MboReplayMarket`, `--crypto-l3` / `ARENA_CRYPTO_L3`): replays a
  real order-by-order feed into the matching engine so agents take a real place in the FIFO
  queue and fill only when they reach the front.
- **Continuous-time order-entry latency** for the live L3 engine — agent orders land after a
  configurable delay (`--l3-latency-us` / `ARENA_L3_LATENCY_US`), enabling the cancel race.
- **Real L2 order-book replay** (`CryptoBookReplayMarket`, `--crypto-book` / `ARENA_CRYPTO_BOOK`):
  trade against recorded market depth with real slippage and queues.
- **Maker/taker fees + fill telemetry**: per-agent maker/taker volume, fees, and maker fraction.
- Sample data: a real Bitstamp BTC/USD L3 capture (`data/btcusd_l3_sample.jsonl`) and recorded
  L2 books; helper recorders under `deploy/` (`fetch_crypto_l3.py`, `fetch_crypto_orderbook.py`).
- Tests for the L3 engine, L3 replay, L2 book replay, and fees/telemetry.

### Fixed
- **PnL/position scaling in the crypto modes.** Crypto quantities are integer micro-units
  (`qty_scale` 1e6 for L3, 1000 for the L2 book), so raw PnL accrued in `cents × qty_scale` and
  the leaderboard reported wildly inflated dollars. PnL is now divided by `qty_scale` to recover
  real cents; `MboReplayMarket.qty_scale` is exposed and a per-mode `pnl_scale` is applied to the
  live snapshot, the `arena_rankings` writes, fee display, and console output. Internal engine
  position is unchanged (risk/liquidation still use raw units).

### Changed
- All server configuration is env-driven (every CLI flag falls back to an `ARENA_*` variable), so
  no custom start command is needed for container deploys.

## [0.1.0]

- Initial release: discrete-time `MatchingEngine` / `OrderBook` with price-time FIFO priority,
  synthetic background agents, a WebSocket server, and a remote-agent client.
