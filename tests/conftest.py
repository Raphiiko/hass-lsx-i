"""Shared fixtures for the KEF LSX test suite."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# Keep standalone fake tests and custom_components imports portable in CI.
TESTS_DIR = Path(__file__).parent
REPOSITORY_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(REPOSITORY_ROOT))
sys.path.insert(0, str(TESTS_DIR))
FakeLsxServer = importlib.import_module("fake_lsx").FakeLsxServer
PLUGIN_AUTOLOAD_DISABLED = os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") == "1"


if (
    importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
    and not PLUGIN_AUTOLOAD_DISABLED
):

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Enable loading integrations from custom_components."""
        return


if PLUGIN_AUTOLOAD_DISABLED:

    @pytest.fixture
    def socket_enabled() -> bool:
        """Allow loopback socket tests when the HA plugin is disabled on Windows."""
        return True


@pytest.fixture
async def fake_lsx_server():
    """Provide a clean function-scoped real-TCP LSX fake."""
    async with FakeLsxServer() as speaker:
        yield speaker
    speaker.assert_clean()
