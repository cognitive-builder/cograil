"""Typed errors. Every module raises these; nothing raises bare Exception."""


class CograilError(Exception):
    """Base class."""


class WorkspaceError(CograilError):
    """A workspace folder is missing or invalid."""


class ProtocolParseError(CograilError):
    """A Protocol Markdown file could not be parsed."""


class ToolNotAllowed(CograilError):
    """The model asked for a tool that the current Step does not whitelist."""


class ToolArgumentError(CograilError):
    """Tool arguments failed schema validation."""


class ToolNotFound(CograilError):
    """No Tool with that name is registered."""


class ToolConfigError(CograilError):
    """A Tool cannot be built: bad dotted path, schema, Connection or MCP server."""


class ToolExecutionError(CograilError):
    """A Tool was invoked with valid arguments and failed."""


class GateRequired(CograilError):
    """A write tool was invoked without the required approval."""


class RunNotPaused(CograilError):
    """The Run is not paused on that Approval, so the Approval cannot resume or expire it."""


class RunEnded(CograilError):
    """The Run has escalated, failed or completed; it does not run again."""


class ApprovalNotAllowed(CograilError):
    """The deciding principal is not the Approval's approver, or is the Run's own principal."""


class AudienceDenied(CograilError):
    """The principal is outside the Protocol's audience."""


class AuthNotConfigured(CograilError):
    """COGRAIL_AUTH or the settings its mode needs are missing or invalid."""


class StoreNotConfigured(CograilError):
    """DATABASE_URL is not set, so there is no RunStore to serve from."""


class LoginDenied(CograilError):
    """The identity provider's answer does not let this person sign in."""


class LoopBudgetExceeded(CograilError):
    """A step hit max_turns or a token or dollar budget without signalling step_complete.

    `bound` names the harness.yaml key that was hit; `used` is None when it cannot be known.
    """

    def __init__(self, message: str, *, bound: str, limit: float, used: float | None) -> None:
        super().__init__(message)
        self.bound = bound
        self.limit = limit
        self.used = used


class DecisionError(CograilError):
    """A decision table is invalid or no rule matched under hit_policy first."""


class KnowledgeSourceError(CograilError):
    """A KnowledgeSource folder, document or ACL file cannot be loaded."""


class ProviderError(CograilError):
    """A model provider call failed, or a FakeProvider script ran out."""


class StoreError(CograilError):
    """A RunStore operation failed."""


class RunNotFound(StoreError):
    """No Run with that id exists in the store."""


class ApprovalNotFound(StoreError):
    """No Approval with that token exists in the store."""


class DuplicateRecord(StoreError):
    """A Run or Approval with that id or token already exists."""


class ApprovalAlreadyDecided(StoreError):
    """An Approval can be decided once; it is no longer pending."""


class ApprovalNotSpendable(StoreError):
    """An Approval is spent once, and only after it was approved."""


class ApprovalRunMismatch(StoreError):
    """The Approval belongs to another Run than the one it is decided with."""


class RunClaimLost(StoreError):
    """Another execution claimed the Run since this one read it; this one must stop."""
