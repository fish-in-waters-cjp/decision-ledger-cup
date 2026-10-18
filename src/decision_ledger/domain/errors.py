"""Domain exceptions.

Adapters raise these and services catch them, so services never import an
SDK or a database driver to recognise a failure.
"""


class DecisionLedgerError(Exception):
    """Base class for every domain error."""


class ParseError(DecisionLedgerError):
    """A source file could not be parsed into segments."""


class LLMTransportError(DecisionLedgerError):
    """The LLM backend failed to return a usable response (timeout, exit code, empty)."""


class LLMOutputError(DecisionLedgerError):
    """The LLM call still failed after the allowed retry (invalid output, or see ``LLMUnavailableError``)."""


class LLMUnavailableError(LLMOutputError):
    """The last attempt failed in transport (timeout, command failure, permission), not in validation."""


class InvalidResolution(DecisionLedgerError):
    """A resolution action is not allowed for this alert or decision state."""


class AlertAlreadyResolved(DecisionLedgerError):
    """The alert is no longer open."""


class NotFound(DecisionLedgerError):
    """A requested entity does not exist."""


class SandboxExpired(NotFound):
    """A sandbox id was handed out, but its ledger is gone (the container that held it was recycled)."""


class DuplicateSource(DecisionLedgerError):
    """A source with the same content hash already exists."""


class DuplicatePrCheck(DecisionLedgerError):
    """A PR check with the same idempotency key already exists."""


class DiffUnavailable(DecisionLedgerError):
    """The PR diff could not be obtained."""


class InvalidTopicOperation(DecisionLedgerError):
    """A topic rename or merge would break an invariant (blank, duplicate, self, already merged, other project)."""


class InvalidProjectOperation(DecisionLedgerError):
    """A project cannot be created as requested (blank name, name or repo already used)."""


class ReplayNotReady(DecisionLedgerError):
    """A demo PR replay was requested before the demo sources it depends on were imported."""


class UsageLimitExceeded(DecisionLedgerError):
    """A daily LLM call limit is reached; the model was not called. Not an ``LLMOutputError`` on purpose."""

    def __init__(self, scope: str, limit: int):
        super().__init__(f"daily {scope} limit of {limit} AI model calls reached")
        self.scope, self.limit = scope, limit


class SandboxLimitExceeded(DecisionLedgerError):
    """Today's sandbox creation limit is reached; no project was created."""


class SchemaVersionMismatch(DecisionLedgerError):
    """The database was created by an incompatible version; it is left untouched."""

    def __init__(self, path: str, found: int, expected: int):
        super().__init__(f"{path}: schema version {found}, expected {expected}")
        self.path, self.found, self.expected = path, found, expected


class Unauthenticated(DecisionLedgerError):
    """The request carries no valid identity token."""


class Forbidden(DecisionLedgerError):
    """The identity is valid but may not perform this operation on this project."""


class ConcurrentModification(DecisionLedgerError):
    """Another writer committed to the same project during this unit of work; nothing was written."""


class UserLimitExceeded(DecisionLedgerError):
    """Today's limit of new signed-in workspaces is reached; no workspace was created."""
