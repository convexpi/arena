"""
market.py — The simulation loop that ties engine + agents together.

- Hidden fundamental value follows a jump-diffusion; informed traders see it.
- Per-agent accounting (cash, position, mark-to-market PnL).
- Scenario hooks: callables fired at chosen ticks (vol shock, liquidity pull...).
- Telemetry: per-tick snapshots + full trade tape, written as CSV.

The step-level helpers (_inject_fundamental, build_state, _collect_orders,
_settle, _record_snapshot) are used by both run() and the WebSocket server
in server.py, which drives the tick loop externally.
"""

from __future__ import annotations
import csv
import math
import random
from dataclasses import dataclass, field

from .engine import MatchingEngine, Order, Side, Trade
from .agents import Agent, InformedTrader, NoiseTrader, MarketState


@dataclass
class Account:
    cash: int = 0          # cents
    position: int = 0      # shares (signed)

    def value(self, mark: float) -> float:
        return self.cash + self.position * mark


class FundamentalValue:
    """Jump-diffusion in log space. Agents trade around this hidden truth."""

    def __init__(self, initial=10_000, drift=0.0, vol_bps=8.0,
                 jump_prob=0.002, jump_bps=150, seed=0,
                 process="gauss", horizon=200_000):
        self.value = float(initial)            # cents
        self.drift = drift
        self.vol_bps = vol_bps
        self.jump_prob = jump_prob
        self.jump_bps = jump_bps
        self.rng = random.Random(seed)
        # process = "gauss" (default, constant-vol jump-diffusion) or "garch" (finmlsim GARCH(1,1)-t
        # path → volatility CLUSTERING, so quiet stretches and turbulent bursts, like a real tape).
        self.process = process
        self._path = None
        self._i = 0
        if process == "garch":
            import finmlsim as fms
            g = fms.simulate.garch(n=int(horizon), dist="t", mu=0.0, seed=seed)
            self._path = g / (g.std() + 1e-12) * (vol_bps / 1e4)   # avg vol = vol_bps, clustering in shape

    def step(self) -> float:
        if self.process == "garch":
            shock = self.drift + float(self._path[self._i % len(self._path)])
            self._i += 1
        else:
            shock = self.rng.gauss(self.drift, self.vol_bps / 1e4)
        if self.rng.random() < self.jump_prob:                     # discrete jumps on top, either mode
            shock += self.rng.choice([-1, 1]) * self.jump_bps / 1e4
        self.value *= math.exp(shock)
        return self.value


class Market:
    def __init__(self, agents: list[Agent], n_ticks=2_000, seed=0,
                 fundamental_kwargs: dict | None = None,
                 maker_fee_bps: float = 0.0, taker_fee_bps: float = 0.0):
        self.engine = MatchingEngine(seed=seed)
        self.agents = agents
        self.n_ticks = n_ticks
        self.fundamental = FundamentalValue(seed=seed + 1,
                                            **(fundamental_kwargs or {}))
        self.accounts = {a.agent_id: Account() for a in agents}
        self.scenarios: dict[int, list] = {}   # tick -> [callables]
        self.snapshots: list[dict] = []
        self._last_tick_trades = []
        # Maker/taker fee schedule (basis points of notional). A negative maker fee is a rebate.
        # Default 0 — fees are opt-in and don't change baseline behavior.
        self.maker_fee_bps = maker_fee_bps
        self.taker_fee_bps = taker_fee_bps
        # Per-agent fill telemetry: maker/taker filled volume and cumulative fees paid (cents).
        self.fill_stats: dict[str, dict] = {}

    # ---- scenario engine -------------------------------------------------
    def at_tick(self, tick: int, fn):
        """Register a scenario: fn(market) fires at the start of `tick`."""
        self.scenarios.setdefault(tick, []).append(fn)
        return self

    # ---- bootstrap -------------------------------------------------------
    def _seed_book(self):
        """Seed initial two-sided liquidity around the fundamental."""
        v = round(self.fundamental.value)
        seeder = "__seed__"
        self.accounts[seeder] = Account()
        for i in range(1, 11):
            self.engine.book.submit(Order(seeder, Side.BUY, 50, price=v - 2 * i), tick=0)
            self.engine.book.submit(Order(seeder, Side.SELL, 50, price=v + 2 * i), tick=0)
        self.engine.last_price = v

    # ---- per-tick helpers (used by both run() and server.py) -------------

    def _inject_fundamental(self, fv: float) -> None:
        """Push the current fundamental value into agents that consume it."""
        for a in self.agents:
            if isinstance(a, InformedTrader):
                a.fundamental = fv
            elif isinstance(a, NoiseTrader):
                a.perceived = fv * (1 + a.rng.gauss(0, 0.004))

    def build_state(self, agent_id: str, tick: int) -> MarketState:
        """Build a MarketState view for one agent. Called by the server for
        both local background agents and remote WebSocket agents."""
        book = self.engine.book
        acct = self.accounts.get(agent_id, Account())
        mine = [(o.order_id, o.side, o.price, o.qty)
                for o in book.live.values() if o.agent_id == agent_id]
        return MarketState(
            tick=tick,
            best_bid=book.best_bid(),
            best_ask=book.best_ask(),
            last_price=self.engine.last_price,
            depth=book.depth(),
            recent_trades=self._last_tick_trades,
            position=acct.position,
            cash=acct.cash,
            my_open_orders=mine,
        )

    def _collect_orders(self, tick: int) -> list[Order]:
        """Call on_tick for every agent in self.agents and return their orders."""
        orders: list[Order] = []
        for a in self.agents:
            state = self.build_state(a.agent_id, tick)
            try:
                orders.extend(a.on_tick(state) or [])
            except Exception as e:
                print(f"[tick {tick}] agent {a.agent_id} error: {e}")
        return orders

    def _record_fill(self, agent_id: str, role: str, qty: int, fee: int) -> None:
        """Accumulate maker/taker volume and fees for one side of a trade."""
        s = self.fill_stats.setdefault(
            agent_id, {"maker_volume": 0, "taker_volume": 0, "fees": 0})
        s[f"{role}_volume"] += qty
        s["fees"] += fee

    def _settle(self, trades: list[Trade]) -> None:
        """Update cash and position for every account involved in trades, apply
        maker/taker fees, record fill telemetry, and fire on_fill callbacks."""
        for t in trades:
            if t.buyer_id in self.accounts:
                self.accounts[t.buyer_id].cash -= t.price * t.qty
                self.accounts[t.buyer_id].position += t.qty
            if t.seller_id in self.accounts:
                self.accounts[t.seller_id].cash += t.price * t.qty
                self.accounts[t.seller_id].position -= t.qty

            # The aggressor (the order that crossed the spread) is the taker; the
            # resting order it hit is the maker.
            taker_id = t.buyer_id if t.aggressor_side == Side.BUY else t.seller_id
            maker_id = t.seller_id if t.aggressor_side == Side.BUY else t.buyer_id
            notional = t.price * t.qty
            taker_fee = round(notional * self.taker_fee_bps / 1e4)
            maker_fee = round(notional * self.maker_fee_bps / 1e4)
            if taker_id in self.accounts:
                self.accounts[taker_id].cash -= taker_fee
            if maker_id in self.accounts:
                self.accounts[maker_id].cash -= maker_fee   # negative fee = rebate
            self._record_fill(taker_id, "taker", t.qty, taker_fee)
            self._record_fill(maker_id, "maker", t.qty, maker_fee)

        agent_map = {a.agent_id: a for a in self.agents}
        for t in trades:
            for aid, side in [(t.buyer_id, Side.BUY), (t.seller_id, Side.SELL)]:
                if aid in agent_map:
                    try:
                        agent_map[aid].on_fill(t, side, t.qty, t.price)
                    except Exception:
                        pass

    def _record_snapshot(self, tick: int, fv: float, trades: list[Trade]) -> None:
        """Append one row to the telemetry snapshot list."""
        book = self.engine.book
        self.snapshots.append({
            "tick": tick,
            "fundamental": round(fv, 2),
            "best_bid": book.best_bid(),
            "best_ask": book.best_ask(),
            "last_price": self.engine.last_price,
            "n_trades": len(trades),
            "volume": sum(t.qty for t in trades),
        })

    # ---- main loop -------------------------------------------------------
    def run(self, verbose_every: int | None = None):
        self._seed_book()
        for tick in range(1, self.n_ticks + 1):
            for fn in self.scenarios.get(tick, []):
                fn(self)
            fv = self.fundamental.step()
            self._inject_fundamental(fv)
            orders = self._collect_orders(tick)
            trades = self.engine.process_tick(tick, orders)
            self._settle(trades)
            self._last_tick_trades = trades
            self._record_snapshot(tick, fv, trades)
            if verbose_every and tick % verbose_every == 0:
                s = self.snapshots[-1]
                print(f"tick {tick:>5}  fv={s['fundamental']:>9}  "
                      f"bid={s['best_bid']}  ask={s['best_ask']}  vol={s['volume']}")
        return self

    # ---- results ---------------------------------------------------------
    def leaderboard(self) -> list[tuple[str, float, int]]:
        mark = self.engine.last_price or self.fundamental.value
        rows = [(aid, acct.value(mark) / 100, acct.position)
                for aid, acct in self.accounts.items() if aid != "__seed__"]
        return sorted(rows, key=lambda r: -r[1])

    def write_telemetry(self, snapshots_path: str, trades_path: str):
        with open(snapshots_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=self.snapshots[0].keys())
            w.writeheader()
            w.writerows(self.snapshots)
        with open(trades_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["tick", "price", "qty", "buyer", "seller", "aggressor"])
            for t in self.engine.trades:
                w.writerow([t.tick, t.price, t.qty, t.buyer_id, t.seller_id,
                            t.aggressor_side.value])
