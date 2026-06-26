"""
server.py — Arena WebSocket server.

Turns the in-process simulator into a persistent, networked arena. Background
agents run locally while student agents connect over WebSocket and participate
in the same market.

Install dependencies:
    pip install websockets

Run:
    python server.py                                        # defaults
    python server.py --tick-interval 0.2                    # fast classroom demo
    python server.py --max-drawdown 500 --position-limit 300  # risk limits on
    python server.py --admin-token secret                   # enable instructor console

Three WebSocket connection types (all on the same port):

  AGENT — participates in trading:
    Client → Server:  {"type": "join",    "agent_id": "<id>"}
    Server → Client:  {"type": "welcome", "agent_id": ..., "tick_interval": ...,
                        "response_deadline": ..., "initial_cash_cents": ...}
    Server → Client:  {"type": "tick",  "tick": N, "best_bid": ..., "best_ask": ...,
                        "last_price": ..., "mid": ..., "depth": {...},
                        "recent_trades": [...], "position": 0, "cash": 0,
                        "my_open_orders": [...]}
    Client → Server:  {"type": "orders", "tick": N, "orders": [...]}
    Server → Client:  {"type": "fill",   "tick": N, "price": ..., "qty": ...,
                        "side": "buy"|"sell", ...}
    Server → Client:  {"type": "eliminated", "tick": N, "reason": "..."}

  OBSERVER — receives market broadcast each tick (viz.py uses this):
    Client → Server:  {"type": "observe"}
    Server → Client:  {"type": "welcome", "mode": "observer"}
    Server → Client:  {"type": "market",  "tick": N, "fundamental": ...,
                        "best_bid": ..., "best_ask": ..., "depth": {...},
                        "recent_trades": [...], "volume": N,
                        "leaderboard": [{...}, ...]}

  ADMIN — instructor sends live scenario commands:
    Client → Server:  {"type": "admin", "token": "<admin_token>"}
    Server → Client:  {"type": "welcome", "mode": "admin"}
    Client → Server:  {"action": "vol_shock",   "kwargs": {"multiplier": 3}}
                      {"action": "calm"}
                      {"action": "price_jump",  "kwargs": {"pct": 0.05}}
                      {"action": "jump_risk",   "kwargs": {"multiplier": 5}}
    Server → Client:  {"type": "ack", "action": "..."}

Order format (inside agent "orders" list):
  Limit:   {"order_type": "limit",  "side": "buy"|"sell", "price": <cents>, "qty": N}
  Market:  {"order_type": "market", "side": "buy"|"sell", "qty": N}
  Cancel:  {"order_type": "cancel", "cancel_id": <order_id>}

Timing: each tick the server sends states to all remote agents, waits
`response_deadline` seconds, then processes the tick with whatever orders
arrived. Orders that miss the deadline are dropped for that tick.
"""

from __future__ import annotations
import asyncio
import json
import argparse
import os
import urllib.request
from typing import Optional

try:
    import websockets
    import websockets.exceptions
except ImportError:
    raise SystemExit("Run:  pip install websockets")

from .engine import Order, OrderType, Side
from .agents import Agent, InformedTrader, NoiseTrader, MomentumTrader, NaiveMarketMaker
from .market import Market, Account
from .risk import RiskEngine
from .crypto_replay import CryptoFeed, CryptoReplayMarket
from .crypto_book_replay import CryptoBookFeed, CryptoBookReplayMarket
from .crypto_l3_replay import MboReplayMarket


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _state_to_json(state) -> str:
    return json.dumps({
        "type": "tick",
        "tick": state.tick,
        "best_bid": state.best_bid,
        "best_ask": state.best_ask,
        "last_price": state.last_price,
        "mid": state.mid,
        "depth": state.depth,
        "recent_trades": [
            {"price": t.price, "qty": t.qty, "aggressor": t.aggressor_side.value}
            for t in state.recent_trades
        ],
        "position": state.position,
        "cash": state.cash,
        "my_open_orders": [
            {"order_id": oid, "side": s.value, "price": p, "qty": q}
            for oid, s, p, q in state.my_open_orders
        ],
    })


def _parse_order(agent_id: str, data: dict) -> Optional[Order]:
    try:
        ot = str(data.get("order_type", "limit")).lower()
        if ot == "cancel":
            return Order(agent_id, Side.BUY, 0,
                         order_type=OrderType.CANCEL, cancel_id=int(data["cancel_id"]))
        side = Side.BUY if str(data["side"]).lower() == "buy" else Side.SELL
        qty = int(data["qty"])
        if ot == "market":
            return Order(agent_id, side, qty, order_type=OrderType.MARKET)
        return Order(agent_id, side, qty, price=int(data["price"]))
    except (KeyError, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Named scenario actions (admin-triggerable)
# ---------------------------------------------------------------------------

def _apply_scenario(market: Market, action: str, kwargs: dict) -> str:
    """Mutate market state and return a human-readable description."""
    if action == "vol_shock":
        mult = float(kwargs.get("multiplier", 3.0))
        market.fundamental.vol_bps *= mult
        return f"vol_shock x{mult:.1f} → vol_bps={market.fundamental.vol_bps:.1f}"
    if action == "calm":
        market.fundamental.vol_bps = 8.0
        return "calm → vol_bps reset to 8.0"
    if action == "price_jump":
        pct = float(kwargs.get("pct", 0.05))
        market.fundamental.value *= (1 + pct)
        return f"price_jump {pct:+.1%} → fv=${market.fundamental.value/100:.2f}"
    if action == "jump_risk":
        mult = float(kwargs.get("multiplier", 5.0))
        market.fundamental.jump_prob *= mult
        return f"jump_risk x{mult:.1f} → p={market.fundamental.jump_prob:.4f}"
    return f"unknown action: {action}"


# ---------------------------------------------------------------------------
# Arena server
# ---------------------------------------------------------------------------

class ArenaServer:
    """
    Drives the market tick loop and multiplexes three WebSocket connection types.

    Background agents (in market.agents) run synchronously each tick.
    Remote agents connect via WebSocket and participate in live ticks.
    Observers receive a market broadcast each tick for visualization.
    Admin connections let the instructor trigger scenarios remotely.

    Risk (optional): if max_drawdown_dollars is set, agents that drop more than
    that amount from their personal peak are force-liquidated and marked
    ELIMINATED on the leaderboard. Survival score = PnL / max_drawdown.
    """

    def __init__(
        self,
        background_agents: list[Agent],
        *,
        tick_interval: float = 1.0,
        response_deadline: float = 0.5,
        n_ticks: Optional[int] = None,
        port: int = 8765,
        seed: int = 1,
        initial_cash: int = 100_000,           # cents ($1000); starting equity per agent
        max_drawdown: Optional[float] = None,  # dollars; None disables risk engine
        position_limit: Optional[int] = None,
        admin_token: Optional[str] = None,
        crypto_data: Optional[str] = None,     # path to OHLCV CSV; enables crypto (price) replay mode
        crypto_book: Optional[str] = None,     # path to L2 JSONL; enables real order-book replay mode
        crypto_l3: Optional[str] = None,       # path to L3 JSONL; enables order-by-order (queue) mode
        maker_fee_bps: float = 0.0,            # maker fee (negative = rebate), bps of notional
        taker_fee_bps: float = 0.0,            # taker fee, bps of notional
    ):
        self.tick_interval = tick_interval
        self.response_deadline = min(response_deadline, tick_interval * 0.9)
        self.n_ticks = n_ticks
        self.port = port
        self.initial_cash = initial_cash   # cents
        self.admin_token = admin_token

        if crypto_l3 is not None:
            self.market: Market = MboReplayMarket(
                background_agents, l3_path=crypto_l3, n_ticks=n_ticks, seed=seed)
            print(f"  [CRYPTO-L3] order-by-order replay: {len(self.market._events):,} events  "  # type: ignore[attr-defined]
                  f"(real FIFO queues + queue-based fills)")
        elif crypto_book is not None:
            book_feed = CryptoBookFeed(crypto_book, cents_per_unit=100, qty_scale=1000, loop=True)
            self.market: Market = CryptoBookReplayMarket(
                background_agents, feed=book_feed, n_ticks=n_ticks or book_feed.n_frames, seed=seed
            )
            meta = book_feed.metadata()
            print(f"  [CRYPTO-BOOK] real order-book replay: {meta['frames']} snapshots, "
                  f"{meta['levels_per_side']} levels/side  "
                  f"mid ${(meta['start_mid'] or 0) / 100:.2f} → ${(meta['end_mid'] or 0) / 100:.2f}")
        elif crypto_data is not None:
            feed = CryptoFeed(crypto_data, cents_per_unit=100, loop=True)
            self.market = CryptoReplayMarket(
                background_agents, feed=feed, n_ticks=n_ticks or feed.n_bars, seed=seed
            )
            meta = feed.metadata()
            print(f"  [CRYPTO] price replay mode: {meta['bars']} bars  "
                  f"${meta['start_price']:.2f} → ${meta['end_price']:.2f}")
        else:
            self.market = Market(background_agents, n_ticks=n_ticks or 999_999, seed=seed)
        # Fee schedule applies in every mode (default 0 = no fees).
        self.market.maker_fee_bps = maker_fee_bps
        self.market.taker_fee_bps = taker_fee_bps
        for a in background_agents:
            self.market.accounts[a.agent_id].cash = initial_cash

        self.risk: Optional[RiskEngine] = None
        if max_drawdown is not None:
            self.risk = RiskEngine(
                max_drawdown_dollars=max_drawdown,
                position_limit=position_limit,
                initial_cash_dollars=initial_cash / 100,
            )

        # Connection registries
        self._remote: dict[str, dict] = {}          # agent_id → {ws, queue, account}
        self._observers: set = set()
        self._lock = asyncio.Lock()

        # Cross-tick state
        self._pending_scenarios: list[tuple[str, dict]] = []
        self._liquidation_queue: list[Order] = []

    # ------------------------------------------------------------------
    # WebSocket connection handlers
    # ------------------------------------------------------------------

    async def _handle_client(self, websocket):
        """Dispatch incoming connections to the appropriate handler."""
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            msg = json.loads(raw)
            conn_type = msg.get("type")
            if conn_type == "join":
                await self._handle_agent(websocket, msg)
            elif conn_type == "observe":
                await self._handle_observer(websocket)
            elif conn_type == "admin":
                await self._handle_admin(websocket, msg)
            else:
                await websocket.close(1002, f"unknown type '{conn_type}'; "
                                            "expected join | observe | admin")
        except (asyncio.TimeoutError, json.JSONDecodeError, KeyError):
            pass

    async def _handle_agent(self, websocket, join_msg: dict):
        agent_id = str(join_msg["agent_id"])
        async with self._lock:
            if agent_id not in self._remote:
                acct = Account(cash=self.initial_cash)
                self._remote[agent_id] = {
                    "ws": websocket,
                    "queue": asyncio.Queue(),
                    "account": acct,
                }
                self.market.accounts[agent_id] = acct
            else:
                # Reconnect: swap socket, preserve account and PnL
                self._remote[agent_id]["ws"] = websocket

        print(f"  [+] {agent_id} joined")
        await websocket.send(json.dumps({
            "type": "welcome",
            "agent_id": agent_id,
            "tick_interval": self.tick_interval,
            "response_deadline": self.response_deadline,
            "initial_cash_cents": self.initial_cash,
        }))
        try:
            async for raw in websocket:
                msg = json.loads(raw)
                if msg.get("type") != "orders":
                    continue
                if self.risk and self.risk.is_eliminated(agent_id):
                    continue  # silently drop orders from eliminated agents
                orders = [
                    o for d in msg.get("orders", [])
                    if (o := _parse_order(agent_id, d)) is not None
                ]
                await self._remote[agent_id]["queue"].put(orders)
        except (websockets.exceptions.ConnectionClosed, json.JSONDecodeError):
            pass
        finally:
            async with self._lock:
                if agent_id in self._remote:
                    self._remote[agent_id]["ws"] = None
            print(f"  [-] {agent_id} disconnected")

    async def _handle_observer(self, websocket):
        async with self._lock:
            self._observers.add(websocket)
        try:
            await websocket.send(json.dumps({"type": "welcome", "mode": "observer"}))
            await websocket.wait_closed()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            async with self._lock:
                self._observers.discard(websocket)

    async def _handle_admin(self, websocket, first_msg: dict):
        if self.admin_token and first_msg.get("token") != self.admin_token:
            await websocket.close(1003, "invalid admin token")
            return
        print("  [ADMIN] instructor connected")
        await websocket.send(json.dumps({"type": "welcome", "mode": "admin"}))
        try:
            async for raw in websocket:
                cmd = json.loads(raw)
                action = cmd.get("action", "")
                kwargs = cmd.get("kwargs", {})
                async with self._lock:
                    self._pending_scenarios.append((action, kwargs))
                await websocket.send(json.dumps({"type": "ack", "action": action}))
        except (websockets.exceptions.ConnectionClosed, json.JSONDecodeError):
            pass
        finally:
            print("  [ADMIN] instructor disconnected")

    # ------------------------------------------------------------------
    # Risk helpers
    # ------------------------------------------------------------------

    def _make_liquidation_orders(self, agent_id: str) -> list[Order]:
        """Cancel all resting orders and flatten the position for an agent."""
        orders = []
        book = self.market.engine.book
        for oid, o in list(book.live.items()):
            if o.agent_id == agent_id:
                orders.append(Order(agent_id, o.side, 0,
                                    order_type=OrderType.CANCEL, cancel_id=oid))
        pos = self.market.accounts[agent_id].position
        if pos != 0:
            side = Side.SELL if pos > 0 else Side.BUY
            orders.append(Order(agent_id, side, abs(pos), order_type=OrderType.MARKET))
        return orders

    # ------------------------------------------------------------------
    # Observer broadcast
    # ------------------------------------------------------------------

    def _leaderboard_snapshot(self, mark: int) -> list[dict]:
        """Build leaderboard rows using the risk engine if active, else plain PnL.
        Each row is annotated with maker/taker fill telemetry."""
        if self.risk:
            rows = self.risk.score(self.market.accounts, mark)
        else:
            rows = []
            for aid, acct in self.market.accounts.items():
                if aid == "__seed__":
                    continue
                pnl = (acct.value(mark) - self.initial_cash) / 100
                rows.append({
                    "agent_id": aid,
                    "pnl": round(pnl, 2),
                    "position": acct.position,
                    "max_drawdown": 0.0,
                    "survival_score": round(pnl, 2),
                    "eliminated": False,
                    "eliminated_tick": None,
                    "elimination_reason": "",
                })
            rows.sort(key=lambda r: -r["pnl"])

        # Annotate with maker/taker volume + fees so the UI can show fill quality.
        for r in rows:
            s = self.market.fill_stats.get(r["agent_id"], {})
            mk, tk = s.get("maker_volume", 0), s.get("taker_volume", 0)
            r["maker_volume"] = mk
            r["taker_volume"] = tk
            r["maker_pct"] = round(100 * mk / (mk + tk), 1) if (mk + tk) else None
            r["fees"] = round(s.get("fees", 0) / 100, 2)   # cents -> dollars
        return rows

    def _make_broadcast(self, tick: int, fv: float, trades: list) -> dict:
        book = self.market.engine.book
        mark = self.market.engine.last_price or round(fv)
        bid, ask = book.best_bid(), book.best_ask()
        return {
            "type": "market",
            "tick": tick,
            "fundamental": round(fv, 2),
            "best_bid": bid,
            "best_ask": ask,
            "last_price": self.market.engine.last_price,
            "mid": (bid + ask) / 2 if bid and ask else None,
            "depth": book.depth(),
            "recent_trades": [
                {"price": t.price, "qty": t.qty, "aggressor": t.aggressor_side.value}
                for t in trades
            ],
            "volume": sum(t.qty for t in trades),
            "leaderboard": self._leaderboard_snapshot(mark),
        }

    async def _broadcast(self, tick: int, fv: float, trades: list):
        async with self._lock:
            obs = set(self._observers)
        if not obs:
            return
        msg = json.dumps(self._make_broadcast(tick, fv, trades))
        results = await asyncio.gather(
            *[ws.send(msg) for ws in obs], return_exceptions=True
        )
        async with self._lock:
            for ws, result in zip(obs, results):
                if isinstance(result, Exception):
                    self._observers.discard(ws)

    # ------------------------------------------------------------------
    # Main tick loop
    # ------------------------------------------------------------------

    async def _run_arena(self):
        self.market._seed_book()
        tick = 0

        while self.n_ticks is None or tick < self.n_ticks:
            tick += 1
            t0 = asyncio.get_event_loop().time()

            # Admin scenarios (queued by _handle_admin between ticks)
            async with self._lock:
                pending = list(self._pending_scenarios)
                self._pending_scenarios.clear()
            for action, kwargs in pending:
                desc = _apply_scenario(self.market, action, kwargs)
                print(f"  [SCENARIO] {desc}")

            # Registered at_tick scenarios
            for fn in self.market.scenarios.get(tick, []):
                fn(self.market)

            fv = self.market.fundamental.step()
            self.market._inject_fundamental(fv)

            # Liquidation orders queued by last tick's risk check run first
            orders: list[Order] = list(self._liquidation_queue)
            self._liquidation_queue.clear()

            # Background (local) agent orders
            orders.extend(self.market._collect_orders(tick))

            # Remote agent orders
            async with self._lock:
                live = {
                    aid: info for aid, info in self._remote.items()
                    if info["ws"] is not None
                }

            if live:
                # Send state to all non-eliminated remote agents
                send_coros = []
                for aid, info in live.items():
                    if self.risk and self.risk.is_eliminated(aid):
                        continue
                    send_coros.append(
                        info["ws"].send(_state_to_json(self.market.build_state(aid, tick)))
                    )
                if send_coros:
                    await asyncio.gather(*send_coros, return_exceptions=True)

                # Wait for responses within the deadline
                await asyncio.sleep(self.response_deadline)

                # Drain order queues
                for info in live.values():
                    q = info["queue"]
                    while not q.empty():
                        orders.extend(q.get_nowait())

            # Match, settle, record
            trades = self.market.engine.process_tick(tick, orders)
            self.market._settle(trades)
            self.market._last_tick_trades = trades
            self.market._record_snapshot(tick, fv, trades)

            # Send fill notifications to remote agents
            if trades and live:
                fill_msgs: dict[str, list[str]] = {}
                for t in trades:
                    for side_str, aid in [("buy", t.buyer_id), ("sell", t.seller_id)]:
                        if aid in live:
                            fill_msgs.setdefault(aid, []).append(json.dumps({
                                "type": "fill",
                                "tick": tick,
                                "price": t.price,
                                "qty": t.qty,
                                "side": side_str,
                                "maker_order_id": t.maker_order_id,
                                "taker_order_id": t.taker_order_id,
                            }))
                await asyncio.gather(
                    *[live[aid]["ws"].send(m)
                      for aid, msgs in fill_msgs.items() for m in msgs],
                    return_exceptions=True,
                )

            # Risk check — queue liquidations for next tick
            if self.risk:
                mark = self.market.engine.last_price or round(fv)
                newly_eliminated = self.risk.check(self.market.accounts, mark, tick)
                for aid in newly_eliminated:
                    rs = self.risk._state[aid]
                    print(f"  [ELIMINATED] {aid} — {rs.elimination_reason}")
                    self._liquidation_queue.extend(self._make_liquidation_orders(aid))
                    if aid in live and live[aid]["ws"]:
                        try:
                            await live[aid]["ws"].send(json.dumps({
                                "type": "eliminated",
                                "tick": tick,
                                "reason": rs.elimination_reason,
                            }))
                        except Exception:
                            pass

            # Broadcast to observers (viz.py)
            await self._broadcast(tick, fv, trades)

            # Push rankings to Supabase (optional — only when env vars are set)
            if (os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")) and os.environ.get("SUPABASE_SESSION_ID"):
                asyncio.ensure_future(self._push_rankings(tick))

            # Console heartbeat
            if tick == 1 or tick % 50 == 0:
                s = self.market.snapshots[-1]
                print(f"tick {tick:>5}  fv={s['fundamental']:>9.2f}  "
                      f"bid={s['best_bid']}  ask={s['best_ask']}  "
                      f"vol={s['volume']}  remote={len(live)}")

            # Pace to tick_interval
            elapsed = asyncio.get_event_loop().time() - t0
            await asyncio.sleep(max(0.0, self.tick_interval - elapsed))

        # Session complete — final leaderboard
        print("\n=== FINAL LEADERBOARD ===")
        mark = self.market.engine.last_price or round(self.market.fundamental.value)
        if self.risk:
            rows = self.risk.score(self.market.accounts, mark)
            print(f"{'agent':<24}{'PnL ($)':>10}{'MaxDD ($)':>10}"
                  f"{'Score':>8}{'Status'}")
            for r in rows:
                elim = (f"  ELIMINATED t={r['eliminated_tick']}"
                        if r["eliminated"] else "  alive")
                print(f"  {r['agent_id']:<22}{r['pnl']:>10.2f}"
                      f"{r['max_drawdown']:>10.2f}{r['survival_score']:>8.3f}{elim}")
        else:
            print(f"{'agent':<24}{'PnL ($)':>12}{'position':>10}")
            for aid, total, pos in self.market.leaderboard():
                pnl = total - self.initial_cash / 100
                print(f"  {aid:<22}{pnl:>12,.2f}{pos:>10}")

    # ------------------------------------------------------------------
    # Supabase leaderboard push (fire-and-forget, non-blocking)
    # ------------------------------------------------------------------

    async def _push_rankings(self, tick: int) -> None:
        session_id = os.environ.get("SUPABASE_SESSION_ID", "")
        url = (os.environ.get("SUPABASE_URL")
               or os.environ.get("NEXT_PUBLIC_SUPABASE_URL", "")).rstrip("/") + "/rest/v1/arena_rankings"
        key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        mark = self.market.engine.last_price or round(self.market.fundamental.value)

        # Per-agent survival scores: the RiskEngine computes them in score()
        # (there is no per-agent survival_score attribute to read directly).
        risk_rows: dict[str, dict] = {}
        if self.risk:
            for r in self.risk.score(self.market.accounts, mark):
                risk_rows[r["agent_id"]] = r

        def _survival(aid):
            r = risk_rows.get(aid)
            return r["survival_score"] if r else None

        def _eliminated(aid):
            r = risk_rows.get(aid)
            if r is not None:
                return r["eliminated"]
            return self.risk.is_eliminated(aid) if self.risk else False

        rows = []
        # Background agents
        for aid, acc in self.market.accounts.items():
            rows.append({
                "session_id": session_id,
                "agent_id": aid,
                "user_id": None,
                "tick": tick,
                "pnl_cents": int(acc.cash + acc.position * mark - self.initial_cash),
                "position": acc.position,
                "survival_score": _survival(aid),
                "eliminated": _eliminated(aid),
            })
        # Remote agents
        async with self._lock:
            remote = dict(self._remote)
        for aid, info in remote.items():
            acc: Account = info["account"]
            rows.append({
                "session_id": session_id,
                "agent_id": aid,
                "user_id": info.get("user_id"),
                "tick": tick,
                "pnl_cents": int(acc.cash + acc.position * mark - self.initial_cash),
                "position": acc.position,
                "survival_score": _survival(aid),
                "eliminated": _eliminated(aid),
            })

        body = json.dumps(rows).encode()
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",   # upsert
        }
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=3)
            )
        except Exception as e:
            # Never crash the arena loop on a DB write failure — but make it
            # visible (a silently-swallowed error here once hid a push bug that
            # left every leaderboard empty).
            print(f"  [rankings push failed: {type(e).__name__}: {e}]")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # HTTP health check (runs alongside the WebSocket server)
    # ------------------------------------------------------------------

    async def _health_handler(self, reader: asyncio.StreamReader,
                               writer: asyncio.StreamWriter) -> None:
        """Minimal HTTP/1.1 server for GET /health — used by Railway uptime checks."""
        try:
            await asyncio.wait_for(reader.read(1024), timeout=2.0)
        except asyncio.TimeoutError:
            writer.close()
            return

        snap = self.market.snapshots[-1] if self.market.snapshots else {}
        body = json.dumps({
            "status": "ok",
            "tick": snap.get("tick", 0),
            "agents": len(self._remote),
            "observers": len(self._observers),
        })
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
            + body
        )
        try:
            writer.write(response.encode())
            await writer.drain()
        finally:
            writer.close()

    async def start(self):
        features = []
        if self.risk:
            features.append(
                f"risk(max_dd=${self.risk.max_drawdown_dollars:.0f}"
                + (f", pos_limit=±{self.risk.position_limit}"
                   if self.risk.position_limit else "")
                + ")"
            )
        if self.admin_token:
            features.append("admin-enabled")

        health_port = self.port + 1   # e.g. 8766 when WebSocket is on 8765

        print(f"Arena  ws://localhost:{self.port}  health http://localhost:{health_port}/health")
        print(f"  tick={self.tick_interval}s  deadline={self.response_deadline}s  "
              f"n_ticks={self.n_ticks or '∞'}  "
              f"initial_cash=${self.initial_cash/100:.0f}")
        if features:
            print(f"  features: {'  '.join(features)}")
        print("Waiting for connections (agents / observers / admin)...\n")

        health_server = await asyncio.start_server(
            self._health_handler, "0.0.0.0", health_port
        )
        async with websockets.serve(self._handle_client, "0.0.0.0", self.port):
            async with health_server:
                await self._run_arena()


# ---------------------------------------------------------------------------
# Default background population
# ---------------------------------------------------------------------------

def _default_background() -> list[Agent]:
    return (
        [NoiseTrader(f"noise_{i}", seed=10 + i) for i in range(8)]
        + [NaiveMarketMaker("market_maker", seed=42)]
        + [MomentumTrader(f"momentum_{i}", seed=77 + i) for i in range(2)]
        + [InformedTrader("informed", seed=99)]
    )


def main():
    import os

    # Every CLI flag falls back to an environment variable, so the whole server can be configured
    # through e.g. Railway Variables with no custom start command. An explicit flag still wins.
    def _env_int(name, default=None):
        v = os.environ.get(name)
        return int(v) if v not in (None, "") else default

    def _env_float(name, default=None):
        v = os.environ.get(name)
        return float(v) if v not in (None, "") else default

    p = argparse.ArgumentParser(description="Arena WebSocket server")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)),
                   help="Listen port. Env: PORT (Railway sets this automatically)")
    p.add_argument("--tick-interval", type=float, default=_env_float("ARENA_TICK_INTERVAL", 1.0),
                   help="Real seconds per tick (default: 1.0). Env: ARENA_TICK_INTERVAL")
    p.add_argument("--response-deadline", type=float, default=_env_float("ARENA_RESPONSE_DEADLINE", 0.5),
                   help="Seconds to wait for remote orders per tick (default: 0.5). Env: ARENA_RESPONSE_DEADLINE")
    p.add_argument("--n-ticks", type=int, default=_env_int("ARENA_N_TICKS"),
                   help="Stop after N ticks (default: infinite). Env: ARENA_N_TICKS (blank = forever)")
    p.add_argument("--seed", type=int, default=_env_int("ARENA_SEED", 1),
                   help="RNG seed (default: 1). Env: ARENA_SEED")
    p.add_argument("--initial-cash", type=int, default=_env_int("ARENA_INITIAL_CASH", 1000),
                   help="Starting equity per agent in dollars (default: 1000). Env: ARENA_INITIAL_CASH")
    p.add_argument("--max-drawdown", type=float, default=_env_float("ARENA_MAX_DRAWDOWN"),
                   help="Max drawdown in dollars before elimination (default: off). Env: ARENA_MAX_DRAWDOWN")
    p.add_argument("--position-limit", type=int, default=_env_int("ARENA_POSITION_LIMIT"),
                   help="Max absolute position before elimination (default: off). Env: ARENA_POSITION_LIMIT")
    p.add_argument("--admin-token", type=str, default=os.environ.get("ARENA_ADMIN_TOKEN"),
                   help="Secret token for admin connections (default: disabled). Env: ARENA_ADMIN_TOKEN")
    p.add_argument("--crypto-data", type=str, default=None,
                   help="Path to OHLCV CSV to replay as the price feed (crypto price mode)")
    p.add_argument("--crypto-book", type=str, default=os.environ.get("ARENA_CRYPTO_BOOK"),
                   help="Path to L2 depth JSONL to replay as a real order book (book mode). "
                        "Env: ARENA_CRYPTO_BOOK")
    p.add_argument("--crypto-l3", type=str, default=os.environ.get("ARENA_CRYPTO_L3"),
                   help="Path to L3 (order-by-order) JSONL for the realistic exchange — real FIFO "
                        "queues + queue-based fills. Env: ARENA_CRYPTO_L3")
    p.add_argument("--maker-fee-bps", type=float, default=float(os.environ.get("ARENA_MAKER_FEE_BPS", 0.0)),
                   help="Maker fee in bps of notional (negative = rebate; default 0). Env: ARENA_MAKER_FEE_BPS")
    p.add_argument("--taker-fee-bps", type=float, default=float(os.environ.get("ARENA_TAKER_FEE_BPS", 0.0)),
                   help="Taker fee in bps of notional (default 0). Env: ARENA_TAKER_FEE_BPS")
    args = p.parse_args()

    server = ArenaServer(
        background_agents=_default_background(),
        tick_interval=args.tick_interval,
        response_deadline=args.response_deadline,
        n_ticks=args.n_ticks,
        port=args.port,
        seed=args.seed,
        initial_cash=args.initial_cash * 100,   # convert dollars → cents
        max_drawdown=args.max_drawdown,
        position_limit=args.position_limit,
        admin_token=args.admin_token,
        crypto_data=args.crypto_data,
        crypto_book=args.crypto_book,
        crypto_l3=args.crypto_l3,
        maker_fee_bps=args.maker_fee_bps,
        taker_fee_bps=args.taker_fee_bps,
    )
    asyncio.run(server.start())


if __name__ == "__main__":
    main()
