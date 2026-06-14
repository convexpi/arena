"""
viz.py — Real-time Arena visualizer for the classroom projector.

Connects to a running Arena server as an observer and renders a live
order-book depth chart, trade tape, and survival-scored leaderboard.

Install dependencies:
    pip install websockets rich

Run alongside the server:
    python viz.py [--server ws://localhost:8765]

Layout (terminal full-screen):
  ┌──────────────────────── ARENA header ────────────────────────────┐
  │  ORDER BOOK (depth chart)    │  TAPE (recent trades)             │
  │  asks (red bars, low→high)   │  ▲ BUY  50 @ $100.22             │
  │  ─── spread 4¢ ───           │  ▼ SELL 20 @ $100.18             │
  │  bids (green bars, high→low) │  ...                              │
  ├──────────────────────────────────────────────────────────────────┤
  │  LEADERBOARD  (survival-scored if risk engine active)            │
  │  agent   PnL   Pos   MaxDD   Score   Status                     │
  └──────────────────────────────────────────────────────────────────┘

Reconnects automatically when the server restarts.
"""

from __future__ import annotations
import asyncio
import json
import argparse
from typing import Optional

try:
    import websockets
    import websockets.exceptions
except ImportError:
    raise SystemExit("Run:  pip install websockets")

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    raise SystemExit("Run:  pip install rich")


console = Console()

_BAR_WIDTH = 18   # max chars for depth bars


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------

def _bar(qty: int, max_qty: int, width: int = _BAR_WIDTH) -> str:
    if max_qty == 0:
        return " " * width
    filled = max(1, round(qty / max_qty * width))
    return "█" * filled + " " * (width - filled)


def render_book(depth: dict) -> Panel:
    bids = sorted(depth.get("bids", []), key=lambda x: -x[0])  # high → low
    asks = sorted(depth.get("asks", []), key=lambda x: -x[0])  # high → low (show reversed)

    all_qtys = [q for _, q in bids + asks]
    max_qty = max(all_qtys, default=1)

    grid = Table.grid(padding=(0, 1))
    grid.add_column(justify="right", min_width=9)   # price
    grid.add_column(min_width=_BAR_WIDTH)            # bar
    grid.add_column(justify="right", min_width=5)   # qty

    for price, qty in asks:
        grid.add_row(
            f"${price/100:.2f}",
            Text(_bar(qty, max_qty), style="red"),
            str(qty),
        )

    spread_row = ""
    if bids and asks:
        spread = asks[-1][0] - bids[0][0]
        spread_row = f"── spread {spread}¢ ──"
    grid.add_row("", Text(spread_row, style="dim"), "")

    for price, qty in bids:
        grid.add_row(
            f"${price/100:.2f}",
            Text(_bar(qty, max_qty), style="green"),
            str(qty),
        )

    return Panel(grid, title="[bold]Order Book[/bold]", border_style="blue")


def render_tape(tape: list, max_rows: int = 14) -> Panel:
    grid = Table.grid(padding=(0, 1))
    grid.add_column(width=7)
    grid.add_column()

    for trade in tape[-max_rows:]:
        agg = trade.get("aggressor", "buy")
        if agg == "buy":
            arrow = Text("▲ BUY ", style="bold green")
        else:
            arrow = Text("▼ SELL", style="bold red")
        grid.add_row(arrow, f"{trade['qty']:>4} @ ${trade['price']/100:.2f}")

    return Panel(grid, title="[bold]Tape[/bold]", border_style="blue")


def render_leaderboard(rows: list) -> Panel:
    has_risk = any(r.get("max_drawdown", 0) > 0 or r.get("eliminated") for r in rows)

    t = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold",
              expand=True)
    t.add_column("Agent", min_width=16, no_wrap=True)
    t.add_column("PnL ($)", justify="right", min_width=10)
    t.add_column("Pos", justify="right", width=6)
    if has_risk:
        t.add_column("MaxDD ($)", justify="right", width=9)
        t.add_column("Score", justify="right", width=7)
    t.add_column("Status", width=14)

    for row in rows:
        pnl = row.get("pnl", 0.0)
        pnl_text = Text(f"{pnl:>+,.2f}", style="bold green" if pnl >= 0 else "bold red")

        if row.get("eliminated"):
            status = Text(f"✗ elim t={row.get('eliminated_tick','?')}", style="dim red")
        else:
            status = Text("● alive", style="green")

        cells: list = [
            row["agent_id"],
            pnl_text,
            str(row.get("position", 0)),
        ]
        if has_risk:
            cells.append(f"{row.get('max_drawdown', 0):.2f}")
            score = row.get("survival_score", 0)
            # Clamp display of the huge penalty for eliminated agents
            score_display = f"{min(score, 999):.2f}" if not row.get("eliminated") else "—"
            cells.append(score_display)
        cells.append(status)
        t.add_row(*cells)

    return Panel(t, title="[bold]Leaderboard[/bold]", border_style="blue")


def render_header(msg: dict) -> Panel:
    tick = msg.get("tick", 0)
    last = msg.get("last_price")
    fv = msg.get("fundamental")
    bid = msg.get("best_bid")
    ask = msg.get("best_ask")
    vol = msg.get("volume", 0)
    n_remote = sum(
        1 for r in msg.get("leaderboard", [])
        if not r["agent_id"].startswith(("noise_", "market_maker", "momentum_", "informed"))
    )

    t = Text()
    t.append("ARENA", style="bold white")
    t.append(f"  tick {tick:>5}", style="dim")
    if last:
        t.append(f"  last ${last/100:.2f}", style="bold yellow")
    if fv:
        t.append(f"  fv ${fv/100:.2f}", style="cyan")  # revealed for teaching
    if bid and ask:
        spread = ask - bid
        t.append(f"  spread {spread}¢", style="dim")
    t.append(f"  vol {vol}", style="dim")
    if n_remote:
        t.append(f"  students {n_remote}", style="bold magenta")

    return Panel(t, border_style="dim", padding=(0, 1))


def build_layout(msg: dict, tape: list) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="mid", ratio=5),
        Layout(name="leaderboard", ratio=4),
    )
    layout["mid"].split_row(
        Layout(name="book"),
        Layout(name="tape"),
    )
    layout["header"].update(render_header(msg))
    layout["book"].update(render_book(msg.get("depth", {})))
    layout["tape"].update(render_tape(tape))
    layout["leaderboard"].update(render_leaderboard(msg.get("leaderboard", [])))
    return layout


# ---------------------------------------------------------------------------
# Observer loop
# ---------------------------------------------------------------------------

async def observe(server: str):
    tape: list[dict] = []

    waiting_panel = Panel(
        Text(f"Waiting for Arena server at {server}...", style="yellow"),
        border_style="dim",
    )

    with Live(waiting_panel, console=console, refresh_per_second=4,
              screen=True) as live:
        while True:
            try:
                async with websockets.connect(server) as ws:
                    await ws.send(json.dumps({"type": "observe"}))
                    raw = await ws.recv()
                    welcome = json.loads(raw)
                    if welcome.get("type") != "welcome":
                        live.update(Panel(f"[red]Unexpected: {welcome}"))
                        return

                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "market":
                            continue
                        tape.extend(msg.get("recent_trades", []))
                        tape = tape[-40:]   # keep last 40 trades across ticks
                        live.update(build_layout(msg, tape))

            except (websockets.exceptions.ConnectionClosed,
                    ConnectionRefusedError, OSError):
                live.update(
                    Panel(Text(f"Reconnecting to {server}...", style="yellow"),
                          border_style="dim")
                )
                await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Arena real-time visualizer")
    p.add_argument("--server", default="ws://localhost:8765",
                   help="Arena server address (default: ws://localhost:8765)")
    args = p.parse_args()
    try:
        asyncio.run(observe(args.server))
    except KeyboardInterrupt:
        console.print("\nViz closed.")


if __name__ == "__main__":
    main()
