"""
agents.py — The agent API and the background population.

THE STUDENT-FACING CONTRACT IS TINY:

    class MyAgent(Agent):
        def on_tick(self, state: MarketState) -> list[Order]:
            ...

`state` gives you the book, last trades, your position/cash, and tick number.
Return a list of Orders (possibly empty). That's the whole API — the same
interface serves a 10-line bot and a trained RL policy.
"""

from __future__ import annotations
import math
import random
from dataclasses import dataclass, field
from typing import Optional

from .engine import Order, OrderType, Side


@dataclass
class MarketState:
    """Read-only view handed to each agent every tick."""
    tick: int
    best_bid: Optional[int]
    best_ask: Optional[int]
    last_price: Optional[int]
    depth: dict                     # {"bids": [(price, qty)...], "asks": [...]}
    recent_trades: list             # trades from the previous tick
    position: int                   # this agent's signed inventory
    cash: int                       # this agent's cash, in cents
    my_open_orders: list            # this agent's resting orders [(order_id, side, price, qty)]

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return self.last_price


class Agent:
    """Base class. Override on_tick; optionally on_fill."""

    def __init__(self, agent_id: str, seed: int = 0):
        self.agent_id = agent_id
        self.rng = random.Random(seed)

    def on_tick(self, state: MarketState) -> list[Order]:
        return []

    def on_fill(self, trade, side: Side, qty: int, price: int):
        """Called when one of this agent's orders trades. Optional."""

    # convenience constructors -------------------------------------------
    def limit(self, side: Side, price: int, qty: int) -> Order:
        return Order(self.agent_id, side, qty, price=int(price))

    def market(self, side: Side, qty: int) -> Order:
        return Order(self.agent_id, side, qty, order_type=OrderType.MARKET)

    def cancel(self, order_id: int) -> Order:
        return Order(self.agent_id, Side.BUY, 0, order_type=OrderType.CANCEL,
                     cancel_id=order_id)


# ---------------------------------------------------------------------------
# Background population
# ---------------------------------------------------------------------------

class NoiseTrader(Agent):
    """Trades around a NOISY perception of fundamental value (injected each
    tick by the simulation). This anchors price discovery: collectively,
    noise traders pull price toward value while individually being wrong."""

    def __init__(self, agent_id, seed=0, intensity=0.3, noise_bps=40):
        super().__init__(agent_id, seed)
        self.intensity = intensity
        self.noise_bps = noise_bps
        self.perceived: Optional[float] = None   # noisy fv, set by Market

    def on_tick(self, state: MarketState) -> list[Order]:
        if self.rng.random() > self.intensity:
            return []
        anchor = self.perceived if self.perceived is not None else state.mid
        if anchor is None:
            return []
        side = Side.BUY if self.rng.random() < 0.5 else Side.SELL
        qty = self.rng.randint(1, 10)
        # cross the spread occasionally; otherwise quote around perceived value
        if state.mid is not None and self.rng.random() < 0.3:
            # only take liquidity in the direction of perceived mispricing
            if anchor > state.mid * 1.001:
                return [self.market(Side.BUY, qty)]
            if anchor < state.mid * 0.999:
                return [self.market(Side.SELL, qty)]
            return []
        offset = anchor * self.rng.uniform(-self.noise_bps, self.noise_bps) / 1e4
        price = max(1, round(anchor + offset))
        return [self.limit(side, price, qty)]


class NaiveMarketMaker(Agent):
    """Quotes a fixed spread around the mid, with crude inventory skew.
    Profitable against noise; bleeds against informed flow — by design,
    that's the adverse-selection lesson."""

    def __init__(self, agent_id, seed=0, half_spread=5, size=20, max_inventory=200):
        super().__init__(agent_id, seed)
        self.half_spread = half_spread
        self.size = size
        self.max_inventory = max_inventory

    def on_tick(self, state: MarketState) -> list[Order]:
        if state.mid is None:
            return []
        orders = [self.cancel(oid) for oid, *_ in state.my_open_orders]
        # inventory skew: long inventory -> shade quotes down to offload
        skew = -round(state.position / self.max_inventory * self.half_spread)
        bid = round(state.mid) - self.half_spread + skew
        ask = round(state.mid) + self.half_spread + skew
        if state.position < self.max_inventory:
            orders.append(self.limit(Side.BUY, bid, self.size))
        if state.position > -self.max_inventory:
            orders.append(self.limit(Side.SELL, ask, self.size))
        return orders


class MomentumTrader(Agent):
    """Buys recent strength, sells recent weakness. Amplifies trends —
    the destabilizing ingredient in flash-crash scenarios."""

    def __init__(self, agent_id, seed=0, lookback=20, threshold_bps=15, size=8,
                 max_pos=300):
        super().__init__(agent_id, seed)
        self.lookback = lookback
        self.threshold_bps = threshold_bps
        self.size = size
        self.max_pos = max_pos
        self.history: list[float] = []

    def on_tick(self, state: MarketState) -> list[Order]:
        if state.mid is None:
            return []
        self.history.append(state.mid)
        if len(self.history) < self.lookback:
            return []
        ret_bps = (self.history[-1] / self.history[-self.lookback] - 1) * 1e4
        if ret_bps > self.threshold_bps and state.position < self.max_pos:
            return [self.market(Side.BUY, self.size)]
        if ret_bps < -self.threshold_bps and state.position > -self.max_pos:
            return [self.market(Side.SELL, self.size)]
        return []


class InformedTrader(Agent):
    """Sees the TRUE fundamental value (injected by the simulation) and
    trades toward it when price deviates. The source of adverse selection."""

    def __init__(self, agent_id, seed=0, edge_bps=20, size=12, max_pos=400):
        super().__init__(agent_id, seed)
        self.edge_bps = edge_bps
        self.size = size
        self.max_pos = max_pos
        self.fundamental: Optional[float] = None   # set by Market each tick

    def on_tick(self, state: MarketState) -> list[Order]:
        if state.mid is None or self.fundamental is None:
            return []
        edge_bps = (self.fundamental / state.mid - 1) * 1e4
        if edge_bps > self.edge_bps and state.position < self.max_pos:
            return [self.market(Side.BUY, self.size)]
        if edge_bps < -self.edge_bps and state.position > -self.max_pos:
            return [self.market(Side.SELL, self.size)]
        return []


# ---------------------------------------------------------------------------
# Example agents for teaching / competition
# ---------------------------------------------------------------------------

class AvellanedaStoikov(Agent):
    """
    Inventory-aware market maker based on Avellaneda & Stoikov (2008).

    "High-frequency trading in a limit order book."
    Quantitative Finance, 8(3), 217-224.

    The key insight: a market maker facing inventory risk should skew quotes
    toward reducing inventory. The reservation price is:

        r = mid - q * γ * σ² * (T - t)

    where q is inventory, γ is risk aversion, σ² is price variance, and
    (T - t) is time remaining. The optimal half-spread is:

        δ = γ * σ² * (T - t) + (2/γ) * ln(1 + γ/κ)

    This implementation uses simplified versions suitable for discrete ticks.

    Parameters
    ----------
    gamma : float
        Risk aversion parameter. Higher → tighter inventory control.
    kappa : float
        Order arrival intensity. Higher → smaller spread.
    size : int
        Quote size per side.
    max_inventory : int
        Hard position limit.
    horizon : int
        Time horizon in ticks (used to scale the inventory penalty).
    """

    def __init__(self, agent_id, seed=0, gamma=0.1, kappa=1.5,
                 size=15, max_inventory=300, horizon=500):
        super().__init__(agent_id, seed)
        self.gamma = gamma
        self.kappa = kappa
        self.size = size
        self.max_inventory = max_inventory
        self.horizon = horizon
        self._vol_window: list[float] = []

    def on_tick(self, state: MarketState) -> list[Order]:
        if state.mid is None:
            return []

        # Estimate variance from recent trades
        if state.recent_trades:
            prices = [t.price for t in state.recent_trades]
            self._vol_window.extend(prices)
        self._vol_window = self._vol_window[-50:]
        if len(self._vol_window) < 5:
            sigma2 = (state.mid * 0.001) ** 2
        else:
            arr = [self._vol_window[i] / self._vol_window[i-1] - 1
                   for i in range(1, len(self._vol_window))]
            sigma2 = float(sum(x**2 for x in arr) / len(arr)) * 252

        t_remaining = max(0.01, (self.horizon - state.tick) / self.horizon)

        # Reservation price: skewed by inventory
        q = state.position / max(1, self.max_inventory)   # normalised [-1, 1]
        reservation = state.mid - q * self.gamma * sigma2 * t_remaining * state.mid

        # Optimal half-spread
        half_spread = max(
            1,
            round(self.gamma * sigma2 * t_remaining * state.mid / 2
                  + (2 / self.gamma) * math.log(1 + self.gamma / self.kappa))
        )

        bid = round(reservation) - half_spread
        ask = round(reservation) + half_spread

        orders = [self.cancel(oid) for oid, *_ in state.my_open_orders]
        if state.position < self.max_inventory:
            orders.append(self.limit(Side.BUY,  max(1, bid), self.size))
        if state.position > -self.max_inventory:
            orders.append(self.limit(Side.SELL, ask, self.size))
        return orders


class TWAPAgent(Agent):
    """
    TWAP (Time-Weighted Average Price) execution agent.

    Splits a target order quantity evenly across a fixed time window,
    executing one child order per tick. Classic institutional execution
    algorithm for minimising market impact.

    Reference: Almgren & Chriss (2001). "Optimal Execution of Portfolio
    Transactions." Journal of Risk.

    Parameters
    ----------
    target_qty : int
        Total quantity to buy (positive) or sell (negative).
    duration : int
        Number of ticks over which to spread execution.
    use_limit : bool
        If True, post limit orders at best bid/ask. If False, use market orders.
    """

    def __init__(self, agent_id, seed=0, target_qty=200, duration=100,
                 use_limit=True):
        super().__init__(agent_id, seed)
        self.target_qty = target_qty
        self.duration = duration
        self.use_limit = use_limit
        self._executed = 0
        self._child_qty = max(1, abs(target_qty) // duration)
        self._side = Side.BUY if target_qty > 0 else Side.SELL

    def on_tick(self, state: MarketState) -> list[Order]:
        if abs(self._executed) >= abs(self.target_qty):
            return []
        remaining = abs(self.target_qty) - abs(self._executed)
        qty = min(self._child_qty, remaining)
        if qty <= 0:
            return []
        if self.use_limit and state.best_bid and state.best_ask:
            price = state.best_ask if self._side == Side.BUY else state.best_bid
            return [self.limit(self._side, price, qty)]
        return [self.market(self._side, qty)]

    def on_fill(self, trade, side, qty, price):
        self._executed += qty


class MeanReversionAgent(Agent):
    """
    Statistical mean-reversion agent: fades sustained price deviations
    from a rolling midpoint average, then unwinds when price reverts.

    The counterpart to MomentumTrader. When these two coexist in the Arena,
    they create interesting dynamics: the mean-reversion agent provides
    stabilising liquidity; the momentum agent amplifies trends. Who wins
    depends on how quickly the fundamental value-anchored agents restore price.

    Reference: Lehmann (1990). "Fads, Martingales, and Market Efficiency."
    Quarterly Journal of Economics.

    Parameters
    ----------
    lookback : int
        Rolling window for mean estimate (ticks).
    entry_bps : float
        Minimum deviation from mean (in bps) to trigger a trade.
    exit_bps : float
        Deviation at which to exit (below this, unwind).
    size : int
        Order size per signal.
    max_pos : int
        Maximum absolute position.
    """

    def __init__(self, agent_id, seed=0, lookback=30, entry_bps=20,
                 exit_bps=5, size=10, max_pos=300):
        super().__init__(agent_id, seed)
        self.lookback = lookback
        self.entry_bps = entry_bps
        self.exit_bps = exit_bps
        self.size = size
        self.max_pos = max_pos
        self._history: list[float] = []

    def on_tick(self, state: MarketState) -> list[Order]:
        if state.mid is None:
            return []
        self._history.append(state.mid)
        if len(self._history) > self.lookback:
            self._history.pop(0)
        if len(self._history) < self.lookback:
            return []

        mean = sum(self._history) / len(self._history)
        dev_bps = (state.mid / mean - 1) * 1e4

        orders: list[Order] = []

        # Entry: price too high → sell; price too low → buy
        if dev_bps > self.entry_bps and state.position > -self.max_pos:
            orders.append(self.market(Side.SELL, self.size))
        elif dev_bps < -self.entry_bps and state.position < self.max_pos:
            orders.append(self.market(Side.BUY, self.size))

        # Exit: price has reverted, unwind
        elif abs(dev_bps) < self.exit_bps:
            if state.position > 0:
                orders.append(self.market(Side.SELL, min(self.size, state.position)))
            elif state.position < 0:
                orders.append(self.market(Side.BUY, min(self.size, -state.position)))

        return orders
