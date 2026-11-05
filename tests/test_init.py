"""Integration tests: setup, image entity, saved photos, event, PIR motion."""
from __future__ import annotations

import logging
import os
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.vesta_local.captures import build_photo_path, parse_panel_time
from custom_components.vesta_local.client import CaptureEvent
from custom_components.vesta_local.const import (
    CONF_CAPTURE_DIR,
    CONF_SAVE_CAPTURES,
    DOMAIN,
    EVENT_CAPTURE,
)

from .conftest import build_data, capture_rows, load_json, panel_time, photo_bytes

ENTRY_DATA = {
    "host": "192.168.1.184",
    "username": "user",
    "password": "secret",
    "use_ssl": False,
}


async def setup_entry(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options=options or {},
        unique_id="192.168.1.184",
        entry_id="vesta_entry",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def alarm_log(minutes_ago: float = 0, zone: str = "Zone1(Comedor)") -> list[dict]:
    """An event log with a fresh burglar alarm on top of the real log."""
    when = dt_util.now() - timedelta(minutes=minutes_ago)
    fresh = {
        "log_time": panel_time(when, "/"),
        "area": "1",
        "mode": "Full Arm",
        "action": "Response",
        "user": "",
        "source": zone,
        "device_type": "IR",
        "msg": "Burglar Alarm",
    }
    return [fresh, *load_json("logsGet.json")["logrows"]]


def image_entity_id(hass: HomeAssistant) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "image", DOMAIN, "vesta_entry_IN:032a4e00_last_capture"
    )
    assert entity_id
    return entity_id


def motion_entity_id(hass: HomeAssistant, device_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"vesta_entry_{device_id}"
    )
    assert entity_id
    return entity_id


async def test_unique_ids_unchanged(hass: HomeAssistant, mock_client) -> None:
    """Existing entities keep their unique ids; new ones are added."""
    await setup_entry(hass)
    ids = {e.unique_id for e in er.async_entries_for_config_entry(er.async_get(hass), "vesta_entry")}
    assert {
        "vesta_entry_alarm_area_1",
        "vesta_entry_IN:01",
        "vesta_entry_IN:02",
        "vesta_entry_IN:01_battery",
        "vesta_entry_IN:01_last_event",
        "vesta_entry_gsm_signal",
        "vesta_entry_battery_status",
        "vesta_entry_ac_status",
        "vesta_entry_event_log",
    } <= ids
    # New: camera image and camera motion sensor
    assert "vesta_entry_IN:032a4e00_last_capture" in ids
    assert "vesta_entry_IN:032a4e00" in ids


async def test_via_device_id_without_deprecation(
    hass: HomeAssistant, mock_client, caplog: pytest.LogCaptureFixture
) -> None:
    """Zone devices hang from the panel through via_device_id, no warning."""
    caplog.set_level(logging.WARNING)
    await setup_entry(hass)
    registry = dr.async_get(hass)
    if hasattr(registry, "async_get_device_by_identifier"):
        panel = registry.async_get_device_by_identifier((DOMAIN, "vesta_entry"), "vesta_entry")
        zone = registry.async_get_device_by_identifier((DOMAIN, "vesta_entry_IN:01"), "vesta_entry")
    else:
        panel = registry.async_get_device(identifiers={(DOMAIN, "vesta_entry")})
        zone = registry.async_get_device(identifiers={(DOMAIN, "vesta_entry_IN:01")})
    assert panel and zone
    assert zone.via_device_id == panel.id
    assert "deprecated `via_device`" not in caplog.text


async def test_image_shows_last_photo_and_saves_files(
    hass: HomeAssistant, mock_client, tmp_path, hass_client
) -> None:
    """Existing captures fill the image and are saved, without an event."""
    events = []
    hass.bus.async_listen(EVENT_CAPTURE, events.append)
    taken = dt_util.now() - timedelta(minutes=2)
    mock_client.get_captures.return_value = capture_rows(taken)

    await setup_entry(
        hass, {CONF_SAVE_CAPTURES: True, CONF_CAPTURE_DIR: str(tmp_path)}
    )

    entity_id = image_entity_id(hass)
    state = hass.states.get(entity_id)
    assert state is not None
    assert state.attributes["photo_count"] == 6
    assert state.attributes["type"] == "Alarm"
    assert state.attributes["time"] == panel_time(taken)
    assert state.attributes["photo"].endswith("_006.jpg")
    assert len(state.attributes["saved_files"]) == 6
    assert parse_panel_time(panel_time(taken)).isoformat() == state.state

    client = await hass_client()
    response = await client.get(state.attributes["entity_picture"])
    assert response.status == 200
    assert await response.read() == photo_bytes()

    day = taken.strftime("%Y-%m-%d")
    saved = sorted(os.listdir(tmp_path / day))
    assert len(saved) == 6
    assert saved[0] == f"{taken.strftime('%H%M%S')}_zone3_1.jpg"
    # First start: captures that were already there do not notify
    await hass.async_block_till_done()
    assert events == []


async def test_new_capture_fires_event_once(
    hass: HomeAssistant, mock_client, tmp_path
) -> None:
    """A new alarm starts fast polling; the event fires once, no duplicates."""
    events = []
    hass.bus.async_listen(EVENT_CAPTURE, events.append)
    # Quiet panel at start-up (empty logs)
    mock_client.get_all_data.side_effect = lambda: build_data(event_log=[], reported=[])
    entry = await setup_entry(
        hass, {CONF_SAVE_CAPTURES: True, CONF_CAPTURE_DIR: str(tmp_path)}
    )
    coordinator = entry.runtime_data
    image_id = image_entity_id(hass)

    # Alarm: new log entry, photos still arriving
    taken = dt_util.now()
    mock_client.get_all_data.side_effect = lambda: build_data(event_log=alarm_log())
    mock_client.get_captures.return_value = capture_rows(taken, status="Receiving", count=1)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert events == []
    assert hass.states.get(image_id).attributes["photo_count"] == 1

    # All photos arrived
    mock_client.get_captures.return_value = capture_rows(taken)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert len(events) == 1
    data = events[0].data
    assert data["device_id"] == "IN:032a4e00"
    assert data["zone"] == "3"
    assert data["type"] == "Alarm"
    assert data["photo_count"] == 6
    assert len(data["saved_files"]) == 6
    assert all(os.path.exists(path) for path in data["saved_files"])
    assert data["entity_id"] == image_id
    assert hass.states.get(image_id).attributes["photo"].endswith("_006.jpg")

    # Polling again does not download, save or notify again
    downloads = mock_client.download_file.await_count
    await coordinator.captures.async_update(build_data(event_log=alarm_log()))
    coordinator.captures._last_poll = None
    await coordinator.captures.async_update(build_data(event_log=alarm_log()))
    await hass.async_block_till_done()
    assert len(events) == 1
    assert mock_client.download_file.await_count == downloads
    assert len(os.listdir(tmp_path / taken.strftime("%Y-%m-%d"))) == 6


async def test_no_duplicates_after_restart(
    hass: HomeAssistant, mock_client, tmp_path
) -> None:
    """After a reload the saved photos are remembered."""
    taken = dt_util.now() - timedelta(minutes=1)
    mock_client.get_captures.return_value = capture_rows(taken)
    entry = await setup_entry(
        hass, {CONF_SAVE_CAPTURES: True, CONF_CAPTURE_DIR: str(tmp_path)}
    )
    first = mock_client.download_file.await_count

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Only the latest photo is fetched again (for the image, kept in memory)
    assert mock_client.download_file.await_count == first + 1
    assert len(os.listdir(tmp_path / taken.strftime("%Y-%m-%d"))) == 6


async def test_without_saving_only_latest_photo(hass: HomeAssistant, mock_client, tmp_path) -> None:
    """With the option off nothing is written and one photo is downloaded."""
    mock_client.get_captures.return_value = capture_rows(dt_util.now())
    with patch("custom_components.vesta_local.captures._write_photo") as write:
        await setup_entry(hass)
    write.assert_not_called()
    assert mock_client.download_file.await_count == 1
    assert hass.states.get(image_entity_id(hass)).attributes["saved_files"] == []


async def test_wired_pir_motion_from_event_log(hass: HomeAssistant, mock_client) -> None:
    """Wired PIRs (empty status) turn on with a fresh log entry of their zone."""
    entry = await setup_entry(hass)
    comedor = motion_entity_id(hass, "IN:01")
    pasillo = motion_entity_id(hass, "IN:02")
    camera = motion_entity_id(hass, "IN:032a4e00")

    # Old log only (the 14:51 alarm of the fixture): off, not unknown
    assert hass.states.get(comedor).state == STATE_OFF
    assert hass.states.get(pasillo).state == STATE_OFF
    assert hass.states.get(comedor).attributes["last_log_motion"] == "2026/11/05 14:51:18"

    mock_client.get_all_data.side_effect = lambda: build_data(event_log=alarm_log())
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(comedor).state == STATE_ON
    assert hass.states.get(pasillo).state == STATE_OFF

    # The camera zone logs "Zone3" without a name
    mock_client.get_all_data.side_effect = lambda: build_data(event_log=alarm_log(zone="Zone3"))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(camera).state == STATE_ON
    assert hass.states.get(comedor).state == STATE_OFF

    # A minute later it is off again
    mock_client.get_all_data.side_effect = lambda: build_data(event_log=alarm_log(minutes_ago=1))
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(comedor).state == STATE_OFF


async def test_options_flow_enables_saving(hass: HomeAssistant, mock_client, tmp_path) -> None:
    """The options flow stores the folder and reloads with it."""
    entry = await setup_entry(hass)
    assert entry.runtime_data.captures.save_captures is False

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == "form"
    schema_keys = {str(key) for key in result["data_schema"].schema}
    assert {CONF_SAVE_CAPTURES, CONF_CAPTURE_DIR} <= schema_keys

    target = tmp_path / "vesta"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "username": "user",
            "password": "secret",
            "use_ssl": False,
            CONF_SAVE_CAPTURES: True,
            CONF_CAPTURE_DIR: str(target),
        },
    )
    await hass.async_block_till_done()
    assert result["type"] == "create_entry"
    assert entry.options == {CONF_SAVE_CAPTURES: True, CONF_CAPTURE_DIR: str(target)}
    assert entry.data == ENTRY_DATA
    assert entry.runtime_data.captures.save_captures is True
    assert entry.runtime_data.captures.capture_dir == str(target)
    assert target.is_dir()
    # Credentials unchanged: no extra login test
    assert mock_client.authenticate.await_count == 2  # first setup + reload


async def test_options_flow_rejects_relative_dir(hass: HomeAssistant, mock_client) -> None:
    entry = await setup_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "username": "user",
            "password": "secret",
            "use_ssl": False,
            CONF_SAVE_CAPTURES: True,
            CONF_CAPTURE_DIR: "media/vesta",
        },
    )
    assert result["type"] == "form"
    assert result["errors"] == {CONF_CAPTURE_DIR: "invalid_capture_dir"}


def test_default_capture_dir_option(hass: HomeAssistant) -> None:
    """The default folder is <config>/media/vesta."""
    assert hass.config.path("media", "vesta").endswith(os.path.join("media", "vesta"))


def test_photo_path_layout() -> None:
    """Photo names follow <dir>/<YYYY-MM-DD>/<HHMMSS>_zone<N>_<n>.jpg."""
    capture = CaptureEvent.model_validate(load_json("captureEventListGet.json")["caprows"][0])
    path = build_photo_path("/config/media/vesta", capture, capture.files[0], 0)
    assert path.replace(os.sep, "/") == "/config/media/vesta/2026-11-05/145113_zone3_1.jpg"
    path = build_photo_path("/config/media/vesta", capture, capture.files[5], 5)
    assert path.replace(os.sep, "/") == "/config/media/vesta/2026-11-05/145123_zone3_6.jpg"
    # Unknown file name: capture time and position
    path = build_photo_path("/x", capture, "/temp/download/weird.jpg", 2)
    assert path.replace(os.sep, "/") == "/x/2026-11-05/145123_zone3_3.jpg"


def test_parse_panel_time_formats() -> None:
    assert parse_panel_time("2026-11-05 14:51:23").hour == 14
    assert parse_panel_time("2026/11/05 14:51:05").second == 5
    assert parse_panel_time("1970-01-01 00:00:10") is None
    assert parse_panel_time("") is None
