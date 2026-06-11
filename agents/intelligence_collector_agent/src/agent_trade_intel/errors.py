class AgentTradeIntelError(Exception):
    """Base exception for the intelligence collector."""


class ConfigError(AgentTradeIntelError):
    """Configuration is invalid."""


class ToolUnavailable(AgentTradeIntelError):
    """A required external tool is unavailable."""


class QueueEmpty(AgentTradeIntelError):
    """No queue message is available."""


class ValidationError(AgentTradeIntelError):
    """Input validation failed."""
