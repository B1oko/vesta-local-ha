"""Camera capture handling for the Vesta/Climax Local integration.

PIR cameras take a burst of photos when the alarm triggers. The panel keeps
them in ``/temp/download/`` (lost on reboot) and lists them through
``captureEventListGet``. This module polls that list, keeps the latest photo
of every camera in memory for the ``image`` entities, optionally saves every
photo to a folder and fires ``vesta_local_capture`` once per capture.
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .client import CaptureEvent, VestaData
from .const import (
    CAPTURE_BURST_DURATION,
    CAPTURE_MAX_COUNT,
    CAPTURE_NOTIFY_MAX_AGE,
    CAPTURE_PARTIAL_AGE,
    CAPTURE_SCAN_INTERVAL,
    CAPTURE_STATUS_DONE,
    CAPTURE_STORE_MAX_CAPTURES,
    CAPTURE_STORE_MAX_FILES,
    CAPTURE_STORE_VERSION,
    CID_TRIGGER_EVENTS,
    DOMAIN,
    EVENT_CAPTURE,
    LOG_MOTION_ACTION,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .client import VestaLocalClient

_LOGGER = logging.getLogger(__name__)

# "032a4e00_2026-11-05_145113_001.jpg" -> date, time, sequence
_FILE_NAME_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})_(\d{6})_(\d+)\.jpe?g$", re.IGNORECASE)


def parse_panel_time(value: str) -> datetime | None:
    """Parse a panel timestamp (local time, no zone) into an aware datetime.

    Captures use "2026-11-05 14:51:23" and the event log "2026/11/05 14:51:05".
    Dates before 2000 mean the panel clock was not set and are ignored.
    """
    value = (value or "").strip().replace("/", "-")
    try:
        naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if naive.year < 2000:
        return None
    return naive.replace(tzinfo=dt_util.get_default_time_zone())


def build_photo_path(
    base_dir: str, capture: CaptureEvent, panel_path: str, index: int
) -> str:
    """Return where a capture photo is saved.

    Layout: ``<base_dir>/<YYYY-MM-DD>/<HHMMSS>_zone<zone>_<n>.jpg``, using the
    timestamp and sequence number in the panel file name when present and the
    capture time otherwise.
    """
    name = panel_path.rsplit("/", 1)[-1]
    match = _FILE_NAME_RE.search(name)
    if match and not match.group(1).startswith(("19", "0")):
        day, hhmmss, seq = match.group(1), match.group(2), int(match.group(3))
    else:
        when = parse_panel_time(capture.time) or dt_util.now()
        day, hhmmss, seq = when.strftime("%Y-%m-%d"), when.strftime("%H%M%S"), index + 1
    zone = re.sub(r"[^0-9A-Za-z]", "", capture.zone) or "x"
    return os.path.join(base_dir, day, f"{hhmmss}_zone{zone}_{seq}.jpg")


def _write_photo(path: str, content: bytes) -> str:
    """Write a photo without overwriting a different existing file.

    Returns the path actually used (a "-2", "-3"... suffix is added if a
    different file already has the name; an identical file is reused).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    root, ext = os.path.splitext(path)
    candidate, counter = path, 1
    while os.path.exists(candidate):
        with open(candidate, "rb") as existing:
            if existing.read() == content:
                return candidate
        counter += 1
        candidate = f"{root}-{counter}{ext}"
    tmp = f"{candidate}.part"
    with open(tmp, "wb") as handle:
        handle.write(content)
    os.replace(tmp, candidate)
    return candidate


@dataclass
class CameraPhoto:
    """Latest photo of one camera, as shown by its image entity."""

    capture: CaptureEvent
    panel_path: str
    content: bytes
    updated: datetime
    saved_files: list[str] = field(default_factory=list)


class CaptureManager:
    """Poll, download, save and announce camera captures."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: VestaLocalClient,
        entry_id: str,
        save_captures: bool,
        capture_dir: str,
    ) -> None:
        """Initialize the capture manager."""
        self.hass = hass
        self.client = client
        self.entry_id = entry_id
        self.save_captures = save_captures
        self.capture_dir = capture_dir

        self.captures: list[CaptureEvent] = []
        self.photos: dict[str, CameraPhoto] = {}
        self.camera_entity_ids: dict[str, str] = {}

        self._store: Store[dict[str, Any]] = Store(
            hass, CAPTURE_STORE_VERSION, f"{DOMAIN}.captures_{entry_id}"
        )
        # file key -> local path of saved photos
        self._saved: dict[str, str] = {}
        # capture keys already announced with the event
        self._notified: list[str] = []
        self._initialized = False

        self._last_poll: float | None = None
        self._burst_until = 0.0
        self._baseline_done = False
        self._last_report_uid: int | None = None
        self._last_log_time: str | None = None

    async def async_load(self) -> None:
        """Load the list of already saved files and announced captures."""
        data = await self._store.async_load() or {}
        self._saved = dict(data.get("saved", {}))
        self._notified = list(data.get("notified", []))
        self._initialized = bool(data.get("initialized", False))

    def _store_data(self) -> dict[str, Any]:
        """Return the bookkeeping to persist (trimmed to bounded sizes)."""
        if len(self._saved) > CAPTURE_STORE_MAX_FILES:
            for key in list(self._saved)[: len(self._saved) - CAPTURE_STORE_MAX_FILES]:
                del self._saved[key]
        if len(self._notified) > CAPTURE_STORE_MAX_CAPTURES:
            self._notified = self._notified[-CAPTURE_STORE_MAX_CAPTURES:]
        return {
            "saved": dict(self._saved),
            "notified": list(self._notified),
            "initialized": self._initialized,
        }

    def _schedule_save(self) -> None:
        """Persist the bookkeeping a few seconds later."""
        self._store.async_delay_save(self._store_data, 5)

    async def async_shutdown(self) -> None:
        """Write the bookkeeping now (on unload/reload)."""
        await self._store.async_save(self._store_data())

    # ------------------------------------------------------------------ polling

    def _new_alarm_activity(self, data: VestaData) -> bool:
        """Return True if new alarm-related events appeared since last poll.

        The first call only records where the logs are.
        """
        baseline = self._baseline_done
        self._baseline_done = True
        found = False

        last_uid = self._last_report_uid
        if data.reported_events:
            self._last_report_uid = max(event.uid for event in data.reported_events)
        if baseline:
            found = any(
                (last_uid is None or event.uid > last_uid)
                and event.new_event == "Trigger"
                and event.cid_event in CID_TRIGGER_EVENTS
                for event in data.reported_events
            )

        last_time = self._last_log_time
        times = [e.log_time for e in data.event_log if e.log_time]
        if times:
            self._last_log_time = max(times)
        if baseline:
            found = found or any(
                (last_time is None or event.log_time > last_time)
                and event.action == LOG_MOTION_ACTION
                for event in data.event_log
            )

        return found

    async def async_update(self, data: VestaData) -> None:
        """Poll captures when due (routinely, or fast after an alarm)."""
        now = time.monotonic()
        if self._new_alarm_activity(data):
            _LOGGER.debug("Alarm activity detected, polling captures for %ss", CAPTURE_BURST_DURATION)
            self._burst_until = now + CAPTURE_BURST_DURATION

        in_burst = now < self._burst_until
        due = (
            self._last_poll is None
            or in_burst
            or now - self._last_poll >= CAPTURE_SCAN_INTERVAL
        )
        if not due:
            return
        self._last_poll = now

        captures = await self.client.get_captures(CAPTURE_MAX_COUNT)
        self.captures = captures
        await self._async_process(captures)

        # Stop the burst early once the newest capture is complete and announced.
        if in_burst and captures:
            newest = max(captures, key=lambda c: c.time)
            if newest.status == CAPTURE_STATUS_DONE and newest.key in self._notified:
                self._burst_until = 0.0

    # --------------------------------------------------------------- processing

    def _file_key(self, capture: CaptureEvent, panel_path: str) -> str:
        return f"{capture.device_id}|{capture.time}|{panel_path.rsplit('/', 1)[-1]}"

    async def _async_save_files(self, capture: CaptureEvent) -> list[str]:
        """Download and save the photos of a capture that are not saved yet."""
        saved: list[str] = []
        for index, panel_path in enumerate(capture.image_files):
            key = self._file_key(capture, panel_path)
            if key in self._saved:
                saved.append(self._saved[key])
                continue
            try:
                content = await self.client.download_file(panel_path)
            except Exception as err:  # noqa: BLE001 - retried on next poll
                _LOGGER.warning("Could not download %s: %s", panel_path, err)
                continue
            target = build_photo_path(self.capture_dir, capture, panel_path, index)
            try:
                path = await self.hass.async_add_executor_job(_write_photo, target, content)
            except OSError as err:
                _LOGGER.error("Could not save capture photo to %s: %s", target, err)
                continue
            self._saved[key] = path
            saved.append(path)
            self._remember_photo(capture, panel_path, content)
        return saved

    def _remember_photo(self, capture: CaptureEvent, panel_path: str, content: bytes) -> None:
        """Keep the bytes if this is the newest photo of its camera."""
        current = self.photos.get(capture.device_id)
        if current and (current.capture.time, current.panel_path) >= (capture.time, panel_path):
            return
        self.photos[capture.device_id] = CameraPhoto(
            capture=capture,
            panel_path=panel_path,
            content=content,
            updated=parse_panel_time(capture.time) or dt_util.utcnow(),
        )

    async def _async_refresh_latest(self, captures: list[CaptureEvent]) -> None:
        """Make sure every camera has the last photo of its newest capture."""
        newest: dict[str, CaptureEvent] = {}
        for capture in captures:
            if not capture.image_files:
                continue
            best = newest.get(capture.device_id)
            if best is None or capture.time > best.time:
                newest[capture.device_id] = capture

        for device_id, capture in newest.items():
            panel_path = capture.image_files[-1]
            current = self.photos.get(device_id)
            if current and current.capture.key == capture.key and current.panel_path == panel_path:
                current.capture = capture  # status / file list may have changed
                continue
            if current and current.capture.time > capture.time:
                continue
            try:
                content = await self.client.download_file(panel_path)
            except Exception as err:  # noqa: BLE001 - retried on next poll
                _LOGGER.warning("Could not download %s: %s", panel_path, err)
                continue
            self.photos[device_id] = CameraPhoto(
                capture=capture,
                panel_path=panel_path,
                content=content,
                updated=parse_panel_time(capture.time) or dt_util.utcnow(),
            )

    def _should_notify(self, capture: CaptureEvent, now: datetime) -> bool:
        if capture.key in self._notified or not capture.image_files:
            return False
        when = parse_panel_time(capture.time)
        age = (now - when).total_seconds() if when else None
        if capture.status == CAPTURE_STATUS_DONE:
            return True
        # Incomplete capture that will not finish: announce what arrived.
        return age is not None and age > CAPTURE_PARTIAL_AGE

    async def _async_process(self, captures: list[CaptureEvent]) -> None:
        """Save new photos, update the latest photo and fire events."""
        now = dt_util.now()
        first_run = not self._initialized
        saved_by_capture: dict[str, list[str]] = {}

        for capture in sorted(captures, key=lambda c: c.time):
            if self.save_captures:
                saved_by_capture[capture.key] = await self._async_save_files(capture)

        await self._async_refresh_latest(captures)

        for capture in sorted(captures, key=lambda c: c.time):
            if not self._should_notify(capture, now):
                continue
            self._notified.append(capture.key)
            when = parse_panel_time(capture.time)
            too_old = when is None or (now - when) > timedelta(seconds=CAPTURE_NOTIFY_MAX_AGE)
            if first_run or too_old:
                # Seen for the first time at start-up or long after it happened:
                # keep it, but do not send a stale alarm notification.
                continue
            saved = saved_by_capture.get(capture.key, [])
            photo = self.photos.get(capture.device_id)
            if photo is not None and photo.capture.key == capture.key:
                photo.saved_files = saved
            self.hass.bus.async_fire(
                EVENT_CAPTURE,
                {
                    "entry_id": self.entry_id,
                    "device_id": capture.device_id,
                    "area": capture.area,
                    "zone": capture.zone,
                    "time": capture.time,
                    "type": capture.type,
                    "status": capture.status,
                    "photo_count": len(capture.image_files),
                    "files": capture.image_files,
                    "saved_files": saved,
                    "entity_id": self.camera_entity_ids.get(capture.device_id),
                },
            )
            _LOGGER.info(
                "Capture from %s (zone %s) at %s: %d photos",
                capture.device_id,
                capture.zone,
                capture.time,
                len(capture.image_files),
            )

        for capture in captures:
            photo = self.photos.get(capture.device_id)
            if photo is not None and photo.capture.key == capture.key and not photo.saved_files:
                photo.saved_files = saved_by_capture.get(capture.key, [])

        self._initialized = True
        self._schedule_save()
