"""Exceptions raised by lcsc2fusion. Every failure path uses one of these, never a bare except."""


class LcscError(Exception):
    """Base class for all lcsc2fusion errors. The message is meant to be shown to the user."""


class NetworkError(LcscError):
    """HTTP transport failed (DNS, timeout, TLS, unexpected status)."""


class RateLimitedError(NetworkError):
    """The server blocked us (HTTP 403/429). Retrying makes sense only after a pause."""

    def __init__(self, message: str, retry_after_s: float = 300.0) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class PartNotFoundError(LcscError):
    """The part id does not exist or has no EasyEDA symbol/footprint data."""


class ParseError(LcscError):
    """EasyEDA JSON had an unexpected structure."""


class ConversionError(LcscError):
    """The parsed part cannot be represented in an Eagle library."""


class LintError(LcscError):
    """The generated library failed the Fusion-compatibility lint."""
