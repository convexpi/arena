"""
market_maker.py — a starter market-making agent for the Arena.

A market maker doesn't bet on direction. It quotes *both* sides of the book — a bid below the mid and
an ask above it — and tries to earn the **spread** (and any maker rebate) as buyers and sellers trade
against it. The risks it must manage are the lessons:

  * Inventory risk — every fill leaves you holding a position exposed to the next price move.
  * Adverse selection — your quote gets hit exactly when the market is about to move against it.
  * Queue position — re-quoting every tick is simple but sends you to the back of the queue.

This agent handles inventory with **quote skewing**: the more inventory you hold, the more it shifts
both quotes against that inventory, so you naturally lean toward flattening. It's deliberately small
and readable — copy it, then improve it (smarter skew, only re-quote when the mid moves to keep queue
priority, widen in fast markets, etc.).

Run it against the real-order-book competition::

    pip install convexpi-arena            # or: pip install websockets, then use this file directly
    python examples/market_maker.py my-handle --server wss://arena-production-e3f1.up.railway.app
"""

from __future__ import annotations
import argparse

from convexpi.arena.client import RemoteAgent, MarketState


class MarketMaker(RemoteAgent):
    def __init__(self, agent_id: str, server: str = RemoteAgent.DEFAULT_SERVER, *,
                 half_spread_bps: float = 8.0,   # how far each quote sits from the mid
                 size: int = 5,                  # quote size per side
                 max_pos: int = 40,              # hard inventory cap
                 max_skew_bps: float = 8.0):     # max quote shift at full inventory
        super().__init__(agent_id, server)
        self.half_spread_bps = half_spread_bps
        self.size = size
        self.max_pos = max_pos
        self.max_skew_bps = max_skew_bps

    def on_tick(self, state: MarketState) -> list[dict]:
        if state.mid is None:
            return []

        orders: list[dict] = []

        # 1. Pull our existing quotes so we can re-quote at the current mid. (Simple but costs queue
        #    priority — a refinement is to only re-quote when the mid has moved enough.)
        for o in state.my_open_orders:
            orders.append(self.cancel(o["order_id"]))

        mid = state.mid
        half = mid * self.half_spread_bps / 1e4

        # 2. Inventory skew: shift both quotes *against* current inventory so fills lean us back to
        #    flat. Long inventory -> lower both quotes (sell more eagerly, buy less); short -> raise.
        skew = (state.position / self.max_pos) * (mid * self.max_skew_bps / 1e4)

        bid_price = round(mid - half - skew)
        ask_price = round(mid + half - skew)

        # 3. Only add liquidity on a side if it wouldn't breach the position cap.
        if state.position < self.max_pos and bid_price > 0:
            orders.append(self.limit("buy", bid_price, self.size))
        if state.position > -self.max_pos:
            orders.append(self.limit("sell", ask_price, self.size))

        return orders

    def on_fill(self, tick: int, price: int, qty: int, side: str) -> None:
        print(f"  fill tick={tick:>5}  {side:>4} {qty} @ ${price / 100:,.2f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Arena starter market maker")
    p.add_argument("agent_id", nargs="?", default="remote_mm", help="Your unique handle")
    p.add_argument("--server", default=RemoteAgent.DEFAULT_SERVER, help="Arena server (wss://… in prod)")
    p.add_argument("--half-spread-bps", type=float, default=8.0)
    p.add_argument("--size", type=int, default=5)
    p.add_argument("--max-pos", type=int, default=40)
    args = p.parse_args()
    MarketMaker(args.agent_id, server=args.server, half_spread_bps=args.half_spread_bps,
                size=args.size, max_pos=args.max_pos).start()
