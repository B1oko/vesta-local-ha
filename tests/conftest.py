"""Shared fixtures for the Vesta/Climax Local tests.

The JSON files in ``tests/fixtures`` are real answers of a Climax Hybrid
Panel Lite (VESTA-388), without credentials.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.vesta_local.client import (
    CaptureEvent,
    DeviceStatus,
    EventLogEntry,
    PanelStatus,
    ReportedEvent,
    VestaData,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/vesta_local."""
    yield


def load_json(name: str) -> dict[str, Any]:
    """Load a panel answer from tests/fixtures."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def photo_bytes() -> bytes:
    """Return a synthetic 320x240 JPEG, the size the camera produces."""
    return (FIXTURES / "test_photo.jpg").read_bytes()


def panel_time(when: datetime, sep: str = "-") -> str:
    """Format a datetime like the panel does (local time, no zone)."""
    return when.strftime(f"%Y{sep}%m{sep}%d %H:%M:%S")


def build_data(
    event_log: list[dict[str, Any]] | None = None,
    reported: list[dict[str, Any]] | None = None,
) -> VestaData:
    """Build VestaData from the fixtures, optionally overriding the logs."""
    panel = PanelStatus.model_validate(load_json("panelCondGet.json")["updates"])
    devices = [
        DeviceStatus.model_validate(row) for row in load_json("deviceListGet.json")["senrows"]
    ]
    logs = event_log if event_log is not None else load_json("logsGet.json")["logrows"]
    rpt = reported if reported is not None else load_json("reportEventListGet.json")["rptrows"]
    return VestaData(
        panel=panel,
        devices=devices,
        event_log=[EventLogEntry.model_validate(row) for row in logs],
        reported_events=sorted(
            (ReportedEvent.model_validate(row) for row in rpt),
            key=lambda e: e.uid,
            reverse=True,
        ),
    )


def capture_rows(when: datetime, status: str = "Done", count: int = 6) -> list[CaptureEvent]:
    """Build a capture like the panel's, taken at ``when``."""
    stamp = when.strftime("%Y-%m-%d_%H%M%S")
    row = load_json("captureEventListGet.json")["caprows"][0] | {
        "time": panel_time(when),
        "status": status,
        "files": [f"/temp/download/032a4e00_{stamp}_{n:03d}.jpg" for n in range(1, count + 1)],
    }
    return [CaptureEvent.model_validate(row)]


@pytest.fixture
def mock_client():
    """Patch the API client used by the integration."""
    with patch("custom_components.vesta_local.VestaLocalClient", autospec=False) as cls:
        client = MagicMock()
        client.host = "192.168.1.184"
        client.authenticate = AsyncMock(return_value=True)
        client.close = AsyncMock()
        client.set_alarm_mode = AsyncMock(return_value=True)
        client.get_all_data = AsyncMock(side_effect=lambda: build_data())
        client.get_captures = AsyncMock(return_value=[])
        client.download_file = AsyncMock(side_effect=lambda path: photo_bytes())
        cls.return_value = client
        yield client
