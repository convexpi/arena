"""
convexpi.arena — discrete-time limit-order-book exchange simulator.

Two ways to use it:

  In-process (no optional deps — just numpy):
      from convexpi.arena import Market, MarketState, Agent
      from convexpi.arena import NoiseTrader, MomentumTrader, InformedTrader

  Networked, against a hosted/live arena (needs the `server` extra — websockets):
      from convexpi.arena import RemoteAgent        # connect to a hosted arena
      python -m convexpi.arena.server               # run the arena server
      python -m convexpi.arena.viz                  # terminal visualizer

The in-process simulator (engine, agents, market, crypto replay) imports with only
numpy installed. The networked pieces (`RemoteAgent`, server, viz) need `websockets`
(and `rich` for viz); install them with `pip install convexpi-arena[server]`. To keep
a bare install usable, `RemoteAgent` is imported lazily (PEP 562) — accessing it is
what triggers the `websockets` requirement, not importing the package.
"""

from .engine import Order, OrderType, Side, Trade
from .agents import (
    Agent, MarketState,
    NoiseTrader, NaiveMarketMaker, MomentumTrader, InformedTrader,
    AvellanedaStoikov, TWAPAgent, MeanReversionAgent,
)
from .market import Market
from .crypto_replay import CryptoFeed, CryptoReplayMarket, load_binance_klines, load_coinbase_candles


def __getattr__(name):
    """Lazily import the networking-dependent names so a bare (numpy-only) install can
    still `import convexpi.arena` and drive the in-process simulator (PEP 562)."""
    if name == "RemoteAgent":
        from .client import RemoteAgent
        return RemoteAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Agent",
    "AvellanedaStoikov",
    "CryptoFeed",
    "CryptoReplayMarket",
    "InformedTrader",
    "Market",
    "MarketState",
    "MeanReversionAgent",
    "MomentumTrader",
    "NaiveMarketMaker",
    "NoiseTrader",
    "Order",
    "OrderType",
    "RemoteAgent",
    "Side",
    "Trade",
    "TWAPAgent",
    "load_binance_klines",
    "load_coinbase_candles",
]
