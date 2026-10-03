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


class GateRequired(CograilError):
    """A write tool was invoked without the required approval."""


class AudienceDenied(CograilError):
    """The principal is outside the Protocol's audience."""


class LoopBudgetExceeded(CograilError):
    """A step hit max_turns or a token or dollar budget without signalling step_complete."""


class DecisionError(CograilError):
    """A decision table is invalid or no rule matched under hit_policy first."""
