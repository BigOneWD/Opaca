"""Deterministic bounded LIMIT policy for the first live PAPER BUY.

A market order has no cash ceiling at the broker. The live-paper BUY path
therefore:

1. takes a fresh canonical print
2. derives a LIMIT from that print plus a configured tolerance
3. computes maximum cash obligation as ``qty × LIMIT`` (never the lower
   print, never ``buying_power``)
4. runs TreasuryGuard against that maximum
5. submits the same LIMIT

SELL handling is conservative: modeled proceeds use the canonical print
with no premium, and Opaca still withholds proceeds until T+1 settlement.
Optimistic sell marks cannot create investable liquidity.
"""

from __future__ import annotations

from decimal import ROUND_UP, Decimal

from opaca.domain.models import Side
from opaca.domain.money import CENT, ZERO, money, positive_money, round_budget, round_money
from opaca.market.errors import QuoteValidationError
from opaca.market.quote import CanonicalMarketPrice

#: BUY limit = canonical × (1 + tolerance), quantized UP to cents so the
#: cash ceiling is never understated. 10 basis points (0.001) is the
#: documented default for liquid T-bill ETFs (SGOV / BIL / SHV). Callers
#: may pass a different non-negative Decimal; a negative tolerance fails
#: closed. Do not treat this constant as an unwritten magic number.
DEFAULT_BUY_LIMIT_TOLERANCE = Decimal("0.001")


def buy_limit_price(
    canonical: Decimal,
    *,
    tolerance: Decimal = DEFAULT_BUY_LIMIT_TOLERANCE,
) -> Decimal:
    """LIMIT price for a BUY. Always >= canonical. Rounded UP to cents."""
    price = positive_money(canonical)
    bound = money(tolerance)
    if bound < ZERO:
        raise QuoteValidationError("BUY limit tolerance must be >= 0")
    raw = price * (Decimal("1") + bound)
    return round_money(raw, rounding=ROUND_UP)


def max_buy_cash_obligation(quantity: Decimal, limit_price: Decimal) -> Decimal:
    """Maximum cash the BUY can consume if filled at the LIMIT.

    Uses the same ``round_budget`` (ROUND_DOWN to cents) as ProposedOrder.notional
    so TreasuryGuard and the submitted order refer to one number. For whole
    shares the LIMIT is already quantized to cents, so qty=1 is exact.
    """
    return round_budget(quantity * positive_money(limit_price))


def sell_modeled_price(quote: CanonicalMarketPrice) -> Decimal:
    """Policy valuation and expected SELL proceeds use the canonical print.

    Never a premium over the quote. Proceeds remain non-investable until the
    derived T+1 settlement date; this function does not create cash.
    """
    return quote.price


def sell_limit_price(quote: CanonicalMarketPrice) -> Decimal:
    """SELL LIMIT equals the canonical print (no optimistic mark-up).

    A SELL limit fills at limit or better. Modeled proceeds still use the
    canonical print, never the better-fill, so liquidity cannot appear
    before fill and T+1 settlement.
    """
    return quote.price.quantize(CENT)


def side_limit_price(
    quote: CanonicalMarketPrice,
    side: Side,
    *,
    tolerance: Decimal = DEFAULT_BUY_LIMIT_TOLERANCE,
) -> Decimal:
    if side is Side.BUY:
        return buy_limit_price(quote.price, tolerance=tolerance)
    return sell_limit_price(quote)
