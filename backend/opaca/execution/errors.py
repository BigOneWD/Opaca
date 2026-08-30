"""Execution-layer errors. Fail closed; unknown ≠ failed."""


class ExecutionError(RuntimeError):
    """Base execution error."""


class IllegalTransitionError(ExecutionError):
    """An order state transition is not permitted."""


class ExecutionBlockedError(ExecutionError):
    """Pre-submission revalidation failed. No broker submit occurred."""


class DuplicateSubmissionError(ExecutionError):
    """A second submit was attempted for an existing logical order."""


class ExecutionInvariantError(ExecutionError):
    """A local execution invariant was violated. Fail closed."""
