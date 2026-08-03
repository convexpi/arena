"""Lot sizing: an agent's `qty` must mean the same economic size on every venue.

The engine matches in whatever integer unit a venue uses, and those differ by orders of magnitude —
shares, thousandths of a coin, millionths. Before `lot_size`, the same background agent emitting the
same `qty` traded ~$12,000 of notional on the L2 book replay and ~$1 on the L3 replay, so the entire
L3 arena moved less than a cent and every published P&L rounded to $0.00.
"""
import pytest

from convexpi.arena.crypto_book_replay import CryptoBookFeed, CryptoBookReplayMarket
from convexpi.arena.crypto_l3_replay import MboReplayMarket
from convexpi.arena.engine import Order, OrderType, Side
from convexpi.arena.market import Market
from convexpi.arena.server import _default_background

L3_PATH = "data/btcusd_l3_sample.jsonl"
BOOK_PATH = "data/btcusd_book.jsonl"


def _l3(**kw):
    return MboReplayMarket(_default_background(), l3_path=L3_PATH, n_ticks=kw.pop("n_ticks", 5),
                           seed=1, **kw)


def _book(n_ticks=5):
    feed = CryptoBookFeed(BOOK_PATH, cents_per_unit=100, qty_scale=1000, loop=True)
    return CryptoBookReplayMarket(_default_background(), feed=feed, n_ticks=n_ticks, seed=1)


def test_share_market_trades_in_whole_units():
    assert Market(_default_background(), n_ticks=5).lot_size == 1


def test_lot_size_tracks_the_venue_unit():
    # One lot is a thousandth of a coin on both crypto venues, whatever the engine counts in.
    assert _book().lot_size == 1                       # engine already counts thousandths
    assert _l3().lot_size == 1000                      # engine counts millionths


def test_orders_are_converted_from_lots_to_engine_units():
    m = _l3()
    out = m.to_engine_qty([Order("a", Side.BUY, 7, price=100)])
    assert out[0].qty == 7 * m.lot_size


def test_conversion_is_identity_when_units_already_match():
    assert _book().to_engine_qty([Order("a", Side.BUY, 7, price=100)])[0].qty == 7


def test_cancels_are_not_scaled():
    """A cancel carries no size — scaling its qty would corrupt the order id path."""
    m = _l3()
    cancel = Order("a", Side.BUY, 0, order_type=OrderType.CANCEL, cancel_id=42)
    out = m.to_engine_qty([cancel])
    assert out[0].qty == 0
    assert out[0].cancel_id == 42


def test_the_two_crypto_venues_trade_comparable_notional():
    """The regression that made arena-l3 look dead: same agents, same lots, 1000x the notional."""
    l3, book = _l3(n_ticks=200), _book(n_ticks=200)
    for m in (l3, book):
        for a in m.accounts.values():
            a.cash = 100_000
        m.run()

    def notional(m, scale):
        mark = m.engine.last_price or 0
        # Position is in engine units; divide by the venue's scale to get natural units.
        return max(abs(acc.position) * mark / scale for acc in m.accounts.values())

    l3_notional = notional(l3, l3.qty_scale)
    book_notional = notional(book, 1000)
    assert l3_notional > 0 and book_notional > 0
    ratio = l3_notional / book_notional
    assert 0.05 < ratio < 20, (
        f"venues differ by {ratio:.4g}x in traded notional — lot sizing is out of step "
        f"(L3 {l3_notional:.0f} vs book {book_notional:.0f})")


def test_l3_publishes_non_zero_pnl():
    """End state: an L3 session must be able to move the published cent-denominated P&L."""
    m = _l3(n_ticks=200)
    for a in m.accounts.values():
        a.cash = 100_000
    m.run()
    mark = m.engine.last_price or 0
    published = [round((acc.cash + acc.position * mark - 100_000) / m.qty_scale)
                 for aid, acc in m.accounts.items() if aid != "__seed__"]
    assert any(p != 0 for p in published), "every agent still publishes $0.00"
