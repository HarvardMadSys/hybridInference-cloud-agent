"""Safe domain failures for owner environment configuration and execution."""


class EnvironmentNotFound(Exception):
    """An owned environment, binding or run does not exist."""


class EnvironmentConflict(Exception):
    """A revision or idempotency key differs from the reviewed input."""

    def __init__(self, message: str, *, current_revision: int | None = None) -> None:
        """Carry only safe revision metadata with a conflict explanation."""
        super().__init__(message)
        self.current_revision = current_revision


class EnvironmentBusy(Exception):
    """The owner already has a nonterminal environment test."""
