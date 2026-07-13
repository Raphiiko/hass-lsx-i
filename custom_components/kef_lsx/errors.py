"""Typed failures for legacy KEF LSX communication."""

from __future__ import annotations


class LsxError(Exception):
    """Base class for expected KEF LSX failures."""

    def __init__(self, message: str = "", *, write_attempted: bool = False) -> None:
        super().__init__(message)
        self.write_attempted = write_attempted


class ConnectRefusedError(LsxError):
    """The endpoint refused a TCP connection."""


class ConnectTimeoutError(LsxError):
    """A TCP connection was not established before its deadline."""


class ResponseTimeoutError(LsxError):
    """The speaker did not provide a complete reply before its deadline."""


class ConnectionLostError(LsxError):
    """The control connection closed or reset during an exchange."""


class MalformedResponseError(LsxError):
    """The speaker returned bytes that are not a valid response stream."""


class CommandRejectedError(LsxError):
    """The speaker returned a well-formed non-success SET status."""

    def __init__(self, status: int, *, write_attempted: bool = False) -> None:
        super().__init__(
            f"Speaker rejected command with status 0x{status:02x}",
            write_attempted=write_attempted,
        )
        self.status = status


class ClientClosedError(LsxError):
    """An operation was submitted after the client closed."""


class QueueBusyError(LsxError):
    """The bounded runtime control queue cannot accept more work."""


class AmbiguousCommandError(LsxError):
    """A non-idempotent command may have reached the speaker."""
