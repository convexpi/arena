"""Maker/taker fee accounting and fill telemetry."""

from convexpi.arena.engine import Order, Side, OrderType
from convexpi.arena.market import Market
from convexpi.arena.agents import Agent, MarketState


class Quiet(Agent):
    def on_tick(self, state: MarketState):
        return []


def _market(**kw):
    m = Market([Quiet("maker"), Quiet("taker")], n_ticks=1, **kw)
    m.accounts["maker"].cash = 1_000_000
    m.accounts["taker"].cash = 1_000_000
    return m


def test_no_fees_by_default():
    m = _market()
    assert m.maker_fee_bps == 0.0 and m.taker_fee_bps == 0.0


def test_maker_taker_volume_recorded():
    m = _market()
    # maker rests a sell; taker crosses with a buy.
    m.engine.book.submit(Order("maker", Side.SELL, 10, price=10_000), tick=1)
    trades = m.engine.book.submit(Order("taker", Side.BUY, 4, price=10_000), tick=1)
    m._settle(trades)
    assert m.fill_stats["maker"]["maker_volume"] == 4
    assert m.fill_stats["maker"]["taker_volume"] == 0
    assert m.fill_stats["taker"]["taker_volume"] == 4
    assert m.fill_stats["taker"]["maker_volume"] == 0


def test_taker_pays_fee_maker_gets_rebate():
    # 5 bps taker fee, -2 bps maker fee (rebate).
    m = _market(maker_fee_bps=-2.0, taker_fee_bps=5.0)
    m.engine.book.submit(Order("maker", Side.SELL, 10, price=10_000), tick=1)
    trades = m.engine.book.submit(Order("taker", Side.BUY, 10, price=10_000), tick=1)
    maker_cash0, taker_cash0 = m.accounts["maker"].cash, m.accounts["taker"].cash
    m._settle(trades)
    notional = 10_000 * 10
    taker_fee = round(notional * 5.0 / 1e4)     # 50 cents
    maker_fee = round(notional * -2.0 / 1e4)    # -20 cents (rebate)
    # taker: pays for shares + fee; maker: receives for shares - fee(=+rebate)
    assert m.accounts["taker"].cash == taker_cash0 - notional - taker_fee
    assert m.accounts["maker"].cash == maker_cash0 + notional - maker_fee
    assert m.fill_stats["taker"]["fees"] == taker_fee
    assert m.fill_stats["maker"]["fees"] == maker_fee
