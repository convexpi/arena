"""
client.py — Remote agent SDK for the Arena.

Students subclass RemoteAgent, override on_tick, and call .start().
The API is intentionally identical to the local Agent class in agents.py
so the same strategy code works both ways.

Install the one non-stdlib dependency (same as server.py):
    pip install websockets

Run the example agent:
    python client.py my_agent_name [--server ws://localhost:8765]

Write your own:
    from client import RemoteAgent

    class MyAgent(RemoteAgent):
        def on_tick(self, state):
            if state.mid and state.position < 50:
                return [self.limit("buy", round(state.mid) - 5, 5)]
            return []

    MyAgent("alice").start()
"""

from __future__ import annotations
import asyncio
import json
import argparse
from dataclasses import dataclass
from typing import Optional

try:
    import websockets
    import websockets.exceptions
except ImportError:
    raise SystemExit("Run:  pip install websockets")


# ---------------------------------------------------------------------------
# MarketState — mirrors agents.py (kept separate so client.py has no
# dependency on the server-side codebase; students run this file standalone)
# ---------------------------------------------------------------------------

@dataclass
class MarketState:
    """Read-only snapshot of the market handed to your agent each tick.

    Prices are in integer cents (e.g., 10000 = $100.00).
    cash is also in cents; divide by 100 for dollars.
    """
    tick: int
    best_bid: Optional[int]
    best_ask: Optional[int]
    last_price: Optional[int]
    depth: dict                 # {"bids": [(price, qty), ...], "asks": [...]}
    recent_trades: list         # [{"price": int, "qty": int, "aggressor": "buy"|"sell"}, ...]
    position: int               # your signed share inventory
    cash: int                   # your cash in cents
    my_open_orders: list        # [{"order_id": int, "side": str, "price": int, "qty": int}, ...]

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return float(self.last_price) if self.last_price is not None else None

    @property
    def spread(self) -> Optional[int]:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    @property
    def pnl_dollars(self) -> Optional[float]:
        """Mark-to-market PnL in dollars (requires a mid price)."""
        if self.mid is None:
            return None
        return (self.cash + self.position * self.mid) / 100


# ---------------------------------------------------------------------------
# RemoteAgent base class
# ---------------------------------------------------------------------------

class RemoteAgent:
    """
    Connect to an Arena server and trade via WebSocket.

    Override on_tick to implement your strategy. Use the limit / market_order
    / cancel helpers to build orders — same names as the local Agent class.

    Timing: each tick the server sends you a MarketState; you have
    `response_deadline` seconds (shown in the welcome message) to reply.
    Orders received after the deadline are dropped for that tick.

    Reconnection: if your script disconnects, restart it with the same
    agent_id — the server preserves your position and cash.
    """

    DEFAULT_SERVER = "ws://localhost:8765"

    def __init__(self, agent_id: str, server: str = DEFAULT_SERVER):
        self.agent_id = agent_id
        self.server = server

    # ------------------------------------------------------------------
    # Override these
    # ------------------------------------------------------------------

    def on_tick(self, state: MarketState) -> list[dict]:
        """Called every tick. Return a list of orders (empty = do nothing)."""
        return []

    def on_fill(self, tick: int, price: int, qty: int, side: str) -> None:
        """Called when one of your orders trades. Override if useful."""

    # ------------------------------------------------------------------
    # Order constructors (return dicts the server understands)
    # ------------------------------------------------------------------

    def limit(self, side: str, price: int, qty: int) -> dict:
        """Post a limit order. side = "buy" or "sell". price in cents."""
        return {"order_type": "limit", "side": side,
                "price": int(price), "qty": int(qty)}

    def market_order(self, side: str, qty: int) -> dict:
        """Send a market order (takes the best available price immediately)."""
        return {"order_type": "market", "side": side, "qty": int(qty)}

    def cancel(self, order_id: int) -> dict:
        """Cancel a resting order by its order_id (from state.my_open_orders)."""
        return {"order_type": "cancel", "cancel_id": int(order_id)}

    # ------------------------------------------------------------------
    # Connection handling (internal)
    # ------------------------------------------------------------------

    async def _run(self):
        print(f"Connecting to {self.server} as '{self.agent_id}' ...")
        async with websockets.connect(self.server) as ws:
            # Register
            await ws.send(json.dumps({"type": "join", "agent_id": self.agent_id}))
            raw = await ws.recv()
            welcome = json.loads(raw)
            if welcome.get("type") != "welcome":
                raise RuntimeError(f"Unexpected server response: {welcome}")
            print(f"Connected.  tick_interval={welcome.get('tick_interval')}s  "
                  f"response_deadline={welcome.get('response_deadline')}s")
            print("Running — Ctrl-C to stop.\n")

            async for raw in ws:
                msg = json.loads(raw)

                if msg["type"] == "tick":
                    state = MarketState(
                        tick=msg["tick"],
                        best_bid=msg["best_bid"],
                        best_ask=msg["best_ask"],
                        last_price=msg["last_price"],
                        depth=msg["depth"],
                        recent_trades=msg["recent_trades"],
                        position=msg["position"],
                        cash=msg["cash"],
                        my_open_orders=msg["my_open_orders"],
                    )
                    try:
                        orders = self.on_tick(state) or []
                    except Exception as e:
                        print(f"[tick {state.tick}] on_tick error: {e}")
                        orders = []
                    await ws.send(json.dumps({
                        "type": "orders",
                        "tick": state.tick,
                        "orders": orders,
                    }))

                elif msg["type"] == "fill":
                    try:
                        self.on_fill(msg["tick"], msg["price"], msg["qty"], msg["side"])
                    except Exception as e:
                        print(f"on_fill error: {e}")

    def start(self):
        """Blocking entry point. Connect and run until Ctrl-C or server closes."""
        try:
            asyncio.run(self._run())
        except KeyboardInterrupt:
            print(f"\n{self.agent_id} stopped.")
        except websockets.exceptions.ConnectionClosed:
            print(f"\nServer closed the connection.")


# ---------------------------------------------------------------------------
# Example agent: the same mean-reversion strategy as demo.py, now remote.
# This is the reference implementation students can copy and modify.
# ---------------------------------------------------------------------------

class MeanReverterAgent(RemoteAgent):
    """
    Fade large deviations from a rolling mid-price average.

    Buy when price is significantly below its recent mean (expecting reversion
    upward); sell when significantly above. Hard position limit prevents runaway
    inventory. This is Mission 0 — a working baseline students can improve on.
    """

    def __init__(self, agent_id: str, server: str = RemoteAgent.DEFAULT_SERVER,
                 lookback: int = 50, entry_bps: float = 25,
                 size: int = 5, max_pos: int = 60):
        super().__init__(agent_id, server)
        self.lookback = lookback
        self.entry_bps = entry_bps
        self.size = size
        self.max_pos = max_pos
        self._mids: list[float] = []

    def on_tick(self, state: MarketState) -> list[dict]:
        if state.mid is None:
            return []
        self._mids.append(state.mid)
        if len(self._mids) < self.lookback:
            return []
        mean = sum(self._mids[-self.lookback:]) / self.lookback
        dev_bps = (state.mid / mean - 1) * 1e4
        if dev_bps > self.entry_bps and state.position > -self.max_pos:
            return [self.market_order("sell", self.size)]
        if dev_bps < -self.entry_bps and state.position < self.max_pos:
            return [self.market_order("buy", self.size)]
        return []

    def on_fill(self, tick: int, price: int, qty: int, side: str) -> None:
        pnl_str = ""
        print(f"  fill tick={tick:>4}  {side:>4} {qty} @ ${price/100:.2f}{pnl_str}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Arena remote agent (example: MeanReverter)")
    p.add_argument("agent_id", nargs="?", default="remote_meanrev",
                   help="Your agent's unique name (default: remote_meanrev)")
    p.add_argument("--server", default=RemoteAgent.DEFAULT_SERVER,
                   help="Arena server address (default: ws://localhost:8765)")
    args = p.parse_args()
    MeanReverterAgent(args.agent_id, server=args.server).start()
