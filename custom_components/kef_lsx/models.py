"""Immutable runtime state exposed to Home Assistant entities."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .protocol import Source


@dataclass(frozen=True, slots=True)
class SpeakerState:
    """Last state confirmed by, or optimistically sent to, the speaker."""

    power_on: bool | None = None
    source: Source | None = None
    volume: int | None = None
    muted: bool | None = None


@dataclass(frozen=True, slots=True)
class CommunicationHealth:
    """Communication health with hysteresis separate from speaker state."""

    available: bool = True
    stale: bool = False
    degraded: bool = False
    consecutive_failures: int = 0
    last_channel_success: datetime | None = None
    last_state_success: datetime | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of the most recently accepted explicit command."""

    command: str
    submitted_at: datetime
    completed_at: datetime | None = None
    write_attempted: bool = False
    acknowledged: bool = False
    verified: bool | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    """Atomic publication from the runtime to all entities."""

    speaker: SpeakerState = SpeakerState()
    health: CommunicationHealth = CommunicationHealth()
    last_command: CommandResult | None = None
