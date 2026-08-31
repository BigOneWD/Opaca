"""Market-data and canonical-price errors. Fail closed; never invent a price."""


class MarketDataError(RuntimeError):
    """Base market-data error."""


class MarketDataUnavailableError(MarketDataError):
    """Quote/trade could not be read. No synthetic fallback exists."""


class QuoteValidationError(MarketDataError):
    """A quote failed symbol, magnitude, timestamp, or freshness validation."""


class StaleQuoteError(QuoteValidationError):
    """Quote source timestamp is older than the configured freshness bound."""


class FutureQuoteError(QuoteValidationError):
    """Quote source timestamp is in the future. Fail closed."""


class PriceBindingError(MarketDataError):
    """Policy valuation price and execution reference price are not bound."""
