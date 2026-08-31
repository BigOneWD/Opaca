"""Regression harness for the pre-193d7a2 suites. NOT part of the closeout suite.

`193d7a21` makes a complete canonical price binding a PRECONDITION of broker
mutation and moves the final freshness check onto a real wall-clock read inside
`_submit_leg`. Suites written before that commit call
`execute_reserved_proposal` with no bindings and a frozen `now`, so at this SHA
they block before the broker and their assertions can no longer observe the
behaviour they were written to attack. Run verbatim, the preserved tree shows
88 failures; 70 of those are purely this call contract.

This module supplies ONLY the new precondition, so that those suites can be
re-read as regression evidence:

  * a canonical binding derived from the SAME `prices` mapping each test already
    passes - BUY at tolerance 0, so the bounded LIMIT equals the print the test
    priced its leg at, exactly as the builder's own `bindings_for_proposal`
    does; and
  * a boundary clock pinned to the test's own `now`.

No assertion is touched and no adversarial check is weakened. A test that
supplies its own bindings keeps them.

HOW IT WAS APPLIED (never in place - the preserved suites are history):

    cp -r <this repo> /tmp/adapted
    cat redteam/closeout_193d7a2/contract_adapter.py >> /tmp/adapted/redteam/conftest.py
    cd /tmp/adapted
    OPACA_BACKEND=/tmp/cl/backend pytest -q redteam/ --ignore=redteam/closeout_193d7a2
    #   -> 918 passed, 18 open markers   (verbatim: 848 passed, 88 failed)

The --ignore is required: the closeout suite must run unshimmed, and applying
this adapter to it makes 15 of its 312 probes fail by construction.

WHAT IT DOES NOT PROVE: it manufactures a "canonical" quote out of
caller-supplied prices and pins the boundary clock. It therefore cannot be used
to judge P1-1 or P1-2, and under it two prelive_11d1cde FINDING probes still
report as artefacts of the harness. Run unshimmed against 193d7a2, both pass.
Those verdicts come from `redteam/closeout_193d7a2/` only.
"""
from decimal import Decimal

import opaca.execution.service as _svc
from opaca.broker.gateway import ASSET_UNIVERSE
from opaca.domain.models import Side
from opaca.market.binding import bind_buy, bind_sell
from opaca.market.quote import QUOTE_SOURCE_LATEST_TRADE, CanonicalMarketPrice

_real_execute = _svc.execute_reserved_proposal


def _derive(proposal, prices, now):
    from datetime import timedelta
    out = {}
    for leg in proposal.legs:
        if leg.symbol not in prices or leg.symbol not in ASSET_UNIVERSE:
            continue
        price = prices[leg.symbol]
        if not isinstance(price, Decimal):
            price = Decimal(str(price))
        try:
            quote = CanonicalMarketPrice(
                symbol=leg.symbol,
                price=price,
                source_timestamp=now - timedelta(seconds=1),
                fetched_at=now,
                source=QUOTE_SOURCE_LATEST_TRADE,
            )
            out[leg.symbol] = (
                bind_buy(quote, leg.quantity, tolerance=Decimal("0"))
                if leg.side is Side.BUY
                else bind_sell(quote, leg.quantity)
            )
        except Exception:
            continue
    return out


def _shim(store, read_gateway, mutate_gateway, proposal, *, now, prices,
          price_bindings=None, **kw):
    bindings = price_bindings if price_bindings is not None else _derive(proposal, prices, now)
    saved = _svc._utc_now
    _svc._utc_now = lambda: now
    try:
        return _real_execute(
            store, read_gateway, mutate_gateway, proposal,
            now=now, prices=prices, price_bindings=bindings, **kw)
    finally:
        _svc._utc_now = saved


_svc.execute_reserved_proposal = _shim
