"""Broker gateway errors. Fail closed; never invent state."""


class BrokerError(RuntimeError):
    """Base broker error."""


class BrokerUnavailableError(BrokerError):
    """Broker could not be reached or did not return a usable response."""


class InvalidBrokerStateError(BrokerError):
    """Broker payload is missing, corrupt, or violates domain boundaries."""


class PaperEnvironmentError(BrokerError):
    """Paper environment could not be verified. Live execution is impossible."""
