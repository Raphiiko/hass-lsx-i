"""Privacy-safe diagnostics for KEF LSX config entries."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant

from . import KefLsxConfigEntry

TO_REDACT = {CONF_HOST}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: KefLsxConfigEntry
) -> dict[str, Any]:
    """Return useful state without an endpoint, unique ID, or secret."""
    return {
        "config": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "runtime": asdict(entry.runtime_data.snapshot),
        "scheduler": {
            "queue_size": entry.runtime_data.queue_size,
            "closing": entry.runtime_data._closing,
            "closed": entry.runtime_data._closed,
            "worker_running": entry.runtime_data._worker is not None,
        },
    }
