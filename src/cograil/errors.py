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


class AudienceDenied(CograilError):
    """The principal is outside the Protocol's audience."""


class LoopBudgetExceeded(CograilError):
    """A step hit max_turns or a token or dollar budget without signalling step_complete."""


class DecisionError(CograilError):
    """A decision table is invalid or no rule matched under hit_policy first."""


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
