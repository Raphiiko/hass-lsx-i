"""Diagnostics redaction tests."""

from __future__ import annotations

from types import SimpleNamespace

from custom_components.kef_lsx.diagnostics import async_get_config_entry_diagnostics
from custom_components.kef_lsx.models import RuntimeSnapshot


async def test_diagnostics_redact_endpoint_and_omit_identifiers() -> None:
    entry = SimpleNamespace(
        data={"host": "192.0.2.1", "port": 50001},
        options={"preferred_wake_source": "Opt"},
        runtime_data=SimpleNamespace(
            snapshot=RuntimeSnapshot(),
            queue_size=0,
            _closing=False,
            _closed=False,
            _worker=None,
        ),
        entry_id="private-entry-id",
        unique_id="private-unique-id",
    )

    diagnostics = await async_get_config_entry_diagnostics(None, entry)
    rendered = repr(diagnostics)
    assert "192.0.2.1" not in rendered
    assert "private-entry-id" not in rendered
    assert "private-unique-id" not in rendered
