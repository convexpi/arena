"""
convexpi.arena — Live limit-order-book trading arena.

Student-facing imports:
    from convexpi.arena import RemoteAgent        # connect to hosted arena
    from convexpi.arena import Agent, MarketState # build local in-process agents

Server / instructor:
    python -m convexpi.arena.server               # run the arena server
    python -m convexpi.arena.viz                  # terminal visualizer
"""

from .engine import Order, OrderType, Side, Trade
from .agents import (
    Agent, MarketState,
    NoiseTrader, NaiveMarketMaker, MomentumTrader, InformedTrader,
    AvellanedaStoikov, TWAPAgent, MeanReversionAgent,
)
from .client import RemoteAgent
from .market import Market
from .crypto_replay import CryptoFeed, CryptoReplayMarket, load_binance_klines, load_coinbase_candles

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
