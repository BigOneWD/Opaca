"""Bind PolicyContext.prices and ProposedOrder.reference_price to one quote.

The red-team defect: a caller could pass TreasuryGuard ``prices['SGOV']=100``
while the executable leg used ``reference_price=0.01``. Those surfaces are
now bound.

Without an explicit ``BoundExecutionPrice`` map, reservation still requires
``prices[symbol] == leg.reference_price``. The mutating path refuses a missing
or empty map: execution eligibility cannot be constructed from two matching
caller-controlled prices.

With bindings (live-paper BUY), both values are derived from the same
``CanonicalMarketPrice``:

* valuation / ``PolicyContext.prices[symbol]`` = canonical print
* BUY ``reference_price`` = bounded LIMIT (maximum cash)
* SELL ``reference_price`` = canonical print (never a premium)

Any mismatch fails closed and cannot reach executable AUTO.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from opaca.domain.models import Proposal, ProposedOrder, Side
from opaca.domain.money import require_positive_decimal, round_quantity
from opaca.market.errors import PriceBindingError
from opaca.market.limit import (
    DEFAULT_BUY_LIMIT_TOLERANCE,
    buy_limit_price,
    max_buy_cash_obligation,
    sell_modeled_price,
)
from opaca.market.quote import CanonicalMarketPrice, require_permitted_symbol
from opaca.policy.client_order_id import deterministic_client_order_id


@dataclass(frozen=True)
class BoundExecutionPrice:
    """One symbol's valuation price and execution reference, from one quote."""

    quote: CanonicalMarketPrice
    valuation_price: Decimal
    reference_price: Decimal
    limit_price: Decimal
    side: Side
    quantity: Decimal
    tolerance: Decimal
    max_cash_obligation: Decimal | None

    def __post_init__(self) -> None:
        require_permitted_symbol(self.quote.symbol)
        object.__setattr__(self, "valuation_price", require_positive_decimal(self.valuation_price))
        object.__setattr__(self, "reference_price", require_positive_decimal(self.reference_price))
        object.__setattr__(self, "limit_price", require_positive_decimal(self.limit_price))
        object.__setattr__(self, "quantity", round_quantity(self.quantity))
        object.__setattr__(self, "tolerance", Decimal(self.tolerance))
        if self.valuation_price != self.quote.price:
            raise PriceBindingError("valuation_price must equal the canonical quote price")
        if self.side is Side.BUY:
            expected_limit = buy_limit_price(self.quote.price, tolerance=self.tolerance)
            if self.limit_price != expected_limit:
                raise PriceBindingError("BUY limit_price is not derived from the canonical quote")
            if self.reference_price != self.limit_price:
                raise PriceBindingError("BUY reference_price must equal the bounded LIMIT")
            expected_cash = max_buy_cash_obligation(self.quantity, self.limit_price)
            if self.max_cash_obligation != expected_cash:
                raise PriceBindingError("BUY max cash obligation does not match qty × LIMIT")
        else:
            if self.reference_price != self.quote.price:
                raise PriceBindingError("SELL reference_price must equal the canonical print")
            if self.limit_price != self.quote.price:
                raise PriceBindingError("SELL limit must not be marked above the canonical print")
            if self.max_cash_obligation is not None:
                raise PriceBindingError("SELL has no BUY cash obligation")


def bind_buy(
    quote: CanonicalMarketPrice,
    quantity: Decimal,
    *,
    tolerance: Decimal = DEFAULT_BUY_LIMIT_TOLERANCE,
) -> BoundExecutionPrice:
    limit = buy_limit_price(quote.price, tolerance=tolerance)
    return BoundExecutionPrice(
        quote=quote,
        valuation_price=quote.price,
        reference_price=limit,
        limit_price=limit,
        side=Side.BUY,
        quantity=quantity,
        tolerance=tolerance,
        max_cash_obligation=max_buy_cash_obligation(quantity, limit),
    )


def bind_sell(quote: CanonicalMarketPrice, quantity: Decimal) -> BoundExecutionPrice:
    modeled = sell_modeled_price(quote)
    return BoundExecutionPrice(
        quote=quote,
        valuation_price=modeled,
        reference_price=modeled,
        limit_price=modeled,
        side=Side.SELL,
        quantity=quantity,
        tolerance=Decimal("0"),
        max_cash_obligation=None,
    )


def policy_prices_from_quotes(
    quotes: Mapping[str, CanonicalMarketPrice],
) -> Mapping[str, Decimal]:
    validated = {require_permitted_symbol(symbol): quote.price for symbol, quote in quotes.items()}
    return MappingProxyType(validated)


def bound_proposed_order(
    proposal_id: str,
    leg_index: int,
    bound: BoundExecutionPrice,
) -> ProposedOrder:
    return ProposedOrder(
        proposal_id=proposal_id,
        leg_index=leg_index,
        symbol=bound.quote.symbol,
        side=bound.side,
        quantity=bound.quantity,
        reference_price=bound.reference_price,
        client_order_id=deterministic_client_order_id(proposal_id, leg_index),
    )


def bind_single_leg_proposal(
    proposal_id: str,
    bound: BoundExecutionPrice,
    quotes: Mapping[str, CanonicalMarketPrice],
) -> tuple[Proposal, Mapping[str, Decimal], Mapping[str, BoundExecutionPrice]]:
    """Live-paper constructor: prices and the leg share one canonical quote."""
    proposal = Proposal(
        proposal_id=proposal_id,
        legs=(bound_proposed_order(proposal_id, 0, bound),),
    )
    prices = policy_prices_from_quotes(quotes)
    bindings: Mapping[str, BoundExecutionPrice] = MappingProxyType({bound.quote.symbol: bound})
    failure = price_binding_failure(proposal, prices, bindings=bindings)
    if failure is not None:
        raise PriceBindingError(failure)
    return proposal, prices, bindings


def price_binding_failure(
    proposal: Proposal,
    prices: Mapping[str, Decimal],
    *,
    bindings: Mapping[str, BoundExecutionPrice] | None = None,
) -> str | None:
    """Return a fail-closed reason, or None when valuation and reference match.

    The 0.01-vs-100 exploit is this function returning a reason so the
    caller cannot reach executable AUTO.
    """
    if not proposal.legs:
        return "proposal has no legs"
    for leg in proposal.legs:
        if leg.symbol not in prices:
            return f"policy price missing for {leg.symbol}; cannot bind execution reference_price"
        valuation = prices[leg.symbol]
        if bindings is None:
            if valuation != leg.reference_price:
                return (
                    f"price binding mismatch for {leg.symbol}: "
                    f"PolicyContext.prices={valuation} "
                    f"reference_price={leg.reference_price}; fail closed"
                )
            continue
        bound = bindings.get(leg.symbol)
        if bound is None:
            return f"canonical binding missing for {leg.symbol}; fail closed"
        if bound.quote.symbol != leg.symbol:
            return f"binding symbol {bound.quote.symbol} != leg {leg.symbol}"
        if bound.valuation_price != valuation:
            return (
                f"policy price {valuation} is not the canonical print "
                f"{bound.valuation_price} for {leg.symbol}; fail closed"
            )
        if bound.quote.price != valuation:
            return f"canonical quote {bound.quote.price} != policy price {valuation}"
        if bound.side != leg.side:
            return f"binding side {bound.side.value} != leg side {leg.side.value}"
        if bound.quantity != leg.quantity:
            return "bound quantity does not match the proposed leg"
        if bound.reference_price != leg.reference_price:
            return (
                f"leg reference_price {leg.reference_price} is not the bound "
                f"execution price {bound.reference_price}; fail closed"
            )
        if leg.side is Side.BUY:
            if bound.limit_price != leg.reference_price:
                return "BUY reference_price is not the bounded LIMIT"
            expected = max_buy_cash_obligation(leg.quantity, bound.limit_price)
            if bound.max_cash_obligation != expected:
                return "BUY max cash obligation disagrees with qty × LIMIT"
            if expected != leg.notional:
                return "TreasuryGuard BUY notional disagrees with qty × LIMIT"
        elif bound.reference_price != bound.quote.price:
            return "SELL reference_price is marked above the canonical print"
    if bindings is not None:
        bound_symbols = frozenset(bindings)
        leg_symbols = frozenset(leg.symbol for leg in proposal.legs)
        extra = bound_symbols - leg_symbols
        if extra:
            return f"bindings supplied for symbols not on the proposal: {sorted(extra)}"
    return None


def require_price_binding(
    proposal: Proposal,
    prices: Mapping[str, Decimal],
    *,
    bindings: Mapping[str, BoundExecutionPrice] | None = None,
) -> None:
    failure = price_binding_failure(proposal, prices, bindings=bindings)
    if failure is not None:
        raise PriceBindingError(failure)
