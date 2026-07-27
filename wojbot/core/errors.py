"""Exception hierarchy for WojBot."""


class WojBotException(Exception):
    """Base class for all WojBot errors."""


class ConfigError(WojBotException):
    """Raised when required configuration is missing or invalid."""
