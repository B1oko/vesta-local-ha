"""Vesta/Climax Local API Client.

This module provides an async client for communicating with Vesta/Climax
alarm panels via their local HTTP CGI interface.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, Field, field_validator

from .const import (
    CAMERA_DEVICE_TYPE_IDS,
    CAMERA_DEVICE_TYPES,
    DEFAULT_TIMEOUT,
    ENDPOINT_CAPTURE_LIST,
    ENDPOINT_DEVICE_LIST,
    ENDPOINT_EVENT_LOG,
    ENDPOINT_LOGIN,
    ENDPOINT_PANEL_SET,
    ENDPOINT_PANEL_STATUS,
    ENDPOINT_REPORTED_EVENTS,
)

_LOGGER = logging.getLogger(__name__)


def _is_session_expired(text: str) -> bool:
    """Return True if the panel answered with its 'Session expired' page."""
    return "session expired" in text[:200].lower()


class DeviceStatus(BaseModel):
    """Represents a device/zone status from the panel.

    Attributes:
        area: The area number the device belongs to.
        zone: The zone number of the device.
        name: Human-readable name of the device.
        type_f: Device type (e.g., "Door Contact", "PIR").
        status: Current status string (e.g., "Door Open", "Door Close").
        battery_ok: True if battery is OK, False if low.
        tamper_ok: True if tamper switch is OK.
        rssi: Signal strength indicator.
        device_id: Unique identifier for the device.
    """

    area: int
    zone: int
    name: str
    type_f: str = Field(alias="type_f")
    status: str
    battery_ok: bool = Field(alias="battery_ok")
    tamper_ok: bool = Field(alias="tamper_ok")
    rssi: str
    device_id: str = Field(alias="id")
    type_id: int | None = Field(default=None, alias="type")

    model_config = {"populate_by_name": True}

    @field_validator("battery_ok", "tamper_ok", mode="before")
    @classmethod
    def parse_binary(cls, value: Any) -> bool:
        """Parse binary string values to boolean."""
        if isinstance(value, bool):
            return value
        return str(value) == "1"

    @field_validator("type_id", mode="before")
    @classmethod
    def parse_type_id(cls, value: Any) -> int | None:
        """Parse the numeric device type, tolerating missing or odd values."""
        try:
            return int(value)
        except (ValueError, TypeError):
            return None

    @property
    def is_camera(self) -> bool:
        """Return True if the device takes photos (PIR camera)."""
        return (
            self.type_f in CAMERA_DEVICE_TYPES
            or self.type_id in CAMERA_DEVICE_TYPE_IDS
        )


class PanelStatus(BaseModel):
    """Represents the panel's current status.

    Attributes:
        mode: Current alarm mode (e.g., "Disarm", "Arm", "Home").
        battery_status: Battery status string.
        gsm_signal: GSM signal strength (0-100).
        ac_failure: True if AC power has failed.
    """

    mode: str = Field(alias="mode_a1")
    battery_status: str = Field(default="", alias="battery")
    gsm_signal: int = Field(default=0, alias="sig_gsm")
    ac_failure: bool = Field(default=False, alias="ac_fail")

    model_config = {"populate_by_name": True}

    @field_validator("ac_failure", mode="before")
    @classmethod
    def parse_ac_failure(cls, value: Any) -> bool:
        """Parse AC failure field to boolean."""
        if isinstance(value, bool):
            return value
        return str(value) == "1"

    @field_validator("gsm_signal", mode="before")
    @classmethod
    def parse_gsm_signal(cls, value: Any) -> int:
        """Parse GSM signal to integer."""
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0


class EventLogEntry(BaseModel):
    """Represents an event log entry from the panel.

    Attributes:
        log_time: Timestamp of the event.
        area: Area number.
        mode: Alarm mode at time of event.
        action: Action/event description.
        user: User name (if applicable).
        source: Source zone/device.
        device_type: Type of device involved.
        msg: Additional message.
    """

    log_time: str = Field(default="")
    area: str = Field(default="")
    mode: str = Field(default="")
    action: str = Field(default="")
    user: str = Field(default="")
    source: str = Field(default="")
    device_type: str = Field(default="")
    msg: str = Field(default="")

    model_config = {"populate_by_name": True}


class ReportedEvent(BaseModel):
    """Represents a reported event from the panel.

    Attributes:
        uid: Unique identifier (increasing) for the event.
        new_event: Event type (e.g., "Trigger", "Restore").
        cid_event: Contact ID event code (e.g., "130", "131", "400").
    """

    uid: int
    new_event: str = Field(default="")
    cid_event: str = Field(default="")

    model_config = {"populate_by_name": True}

    @field_validator("uid", mode="before")
    @classmethod
    def parse_uid(cls, value: Any) -> int:
        """Parse uid to integer."""
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0


class CaptureEvent(BaseModel):
    """Represents a camera capture (a set of photos) stored on the panel.

    Attributes:
        uid: Panel row id; only valid for deleting and reused across reboots.
        device_id: Camera device id (e.g. "IN:032a4e00").
        area: Area number as text.
        zone: Zone number as text.
        time: Panel local time "YYYY-MM-DD HH:MM:SS" (no time zone).
        status: Transfer status; "Done" once every photo has arrived.
        type: Why the photos were taken (e.g. "Alarm").
        comment: Panel comment (transfer details).
        files: Panel paths of the files, e.g. "/temp/download/x_001.jpg".
    """

    uid: int = 0
    device_id: str = ""
    area: str = ""
    zone: str = ""
    time: str = ""
    status: str = ""
    type: str = ""
    comment: str = ""
    files: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    @field_validator("uid", mode="before")
    @classmethod
    def parse_uid(cls, value: Any) -> int:
        """Parse uid to integer."""
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0

    @field_validator("area", "zone", "time", "status", "type", "comment", "device_id", mode="before")
    @classmethod
    def parse_text(cls, value: Any) -> str:
        """Coerce scalar fields to text (the panel mixes numbers and strings)."""
        return "" if value is None else str(value)

    @field_validator("files", mode="before")
    @classmethod
    def parse_files(cls, value: Any) -> list[str]:
        """Keep only non-empty file paths."""
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item).strip()]

    @property
    def image_files(self) -> list[str]:
        """Return the JPEG files of the capture (videos are ignored)."""
        return [f for f in self.files if f.lower().endswith((".jpg", ".jpeg"))]

    @property
    def key(self) -> str:
        """Return a stable identity for the capture.

        The panel's uid restarts from 0 and is reused, so the capture is
        identified by camera, time and first file name instead.
        """
        first = self.files[0].rsplit("/", 1)[-1] if self.files else ""
        return f"{self.device_id}|{self.time}|{first}"


@dataclass
class VestaData:
    """Container for all data retrieved from the panel.

    Attributes:
        panel: The current panel status.
        devices: List of all device statuses.
        event_log: List of recent event log entries.
        reported_events: List of reported events for triggered state detection.
    """

    panel: PanelStatus
    devices: list[DeviceStatus]
    event_log: list[EventLogEntry]
    reported_events: list[ReportedEvent]


class VestaAuthenticationError(Exception):
    """Raised when authentication fails."""


class VestaConnectionError(Exception):
    """Raised when connection to panel fails."""


class VestaApiError(Exception):
    """Raised when API returns an error."""


class VestaLocalClient:
    """Async client for Vesta/Climax local API.

    This client handles all communication with the alarm panel's local
    HTTP interface using httpx with Digest Auth support.

    Attributes:
        host: The hostname or IP address of the panel.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_ssl: bool = False,
        use_ssl: bool = False,
    ) -> None:
        """Initialize the Vesta Local Client.

        Args:
            host: Hostname or IP address of the panel.
            username: Username for authentication.
            password: Password for authentication.
            verify_ssl: Whether to verify SSL certificates. Default False for
                self-signed certs common on local panels.
            use_ssl: Whether to use HTTPS. Default False (use HTTP).
        """
        self._host = host
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._use_ssl = use_ssl
        self._client: httpx.AsyncClient | None = None

        protocol = "https" if use_ssl else "http"
        self._root_url = f"{protocol}://{host}"
        self._base_url = f"{self._root_url}/action"
        self._headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{protocol}://{host}/",
            "Accept": "application/json",
        }

    @property
    def host(self) -> str:
        """Return the host address."""
        return self._host

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the httpx client.

        Returns:
            The httpx AsyncClient instance.
        """
        if self._client is None or self._client.is_closed:
            # Use Basic Auth for Vesta/Climax panels
            auth = httpx.BasicAuth(self._username, self._password)

            self._client = httpx.AsyncClient(
                auth=auth,
                headers=self._headers,
                timeout=httpx.Timeout(DEFAULT_TIMEOUT),
                verify=self._verify_ssl,
                follow_redirects=False,
            )

        return self._client

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        retry_count: int = 2,
    ) -> dict[str, Any]:
        """Make an HTTP request to the panel.

        Args:
            method: HTTP method (GET, POST).
            endpoint: API endpoint (without /action/ prefix).
            data: Optional form data for POST requests.
            retry_count: Number of retries on failure.

        Returns:
            The JSON response as a dictionary.

        Raises:
            VestaAuthenticationError: If authentication fails (401).
            VestaConnectionError: If connection fails.
            VestaApiError: If the API returns an error.
        """
        url = f"{self._base_url}/{endpoint}"
        client = await self._get_client()

        last_error: Exception | None = None

        for attempt in range(retry_count + 1):
            try:
                if method.upper() == "GET":
                    response = await client.get(url)
                else:
                    response = await client.post(url, data=data)

                if response.status_code == 401:
                    raise VestaAuthenticationError(
                        f"Authentication failed for {self._host}"
                    )

                if response.status_code >= 400:
                    raise VestaApiError(
                        f"API error {response.status_code}: {response.text}"
                    )

                # Check if response is JSON
                content_type = response.headers.get("content-type", "")
                if "application/json" not in content_type and "text/json" not in content_type:
                    # Try to parse as JSON anyway (some panels don't set content-type correctly)
                    try:
                        return response.json()
                    except Exception:
                        # The panel sometimes answers "Access Denied: Session
                        # expired!" as plain text; the next request works.
                        if _is_session_expired(response.text) and attempt < retry_count:
                            _LOGGER.debug(
                                "Panel session expired, retrying %s (attempt %d/%d)",
                                endpoint,
                                attempt + 1,
                                retry_count + 1,
                            )
                            last_error = VestaApiError("Session expired")
                            continue
                        raise VestaApiError(
                            f"Unexpected response type '{content_type}' from {url}: {response.text[:200]}"
                        )

                return response.json()

            except httpx.ConnectError as err:
                last_error = VestaConnectionError(
                    f"Connection to {self._host} failed: {err}"
                )
                if attempt < retry_count:
                    wait_time = 2**attempt
                    _LOGGER.debug(
                        "Request failed, retrying in %ds (attempt %d/%d): %s",
                        wait_time,
                        attempt + 1,
                        retry_count + 1,
                        err,
                    )
                    await asyncio.sleep(wait_time)
                continue

            except httpx.TimeoutException:
                last_error = VestaConnectionError(
                    f"Connection to {self._host} timed out"
                )
                if attempt < retry_count:
                    wait_time = 2**attempt
                    _LOGGER.debug(
                        "Request timed out, retrying in %ds (attempt %d/%d)",
                        wait_time,
                        attempt + 1,
                        retry_count + 1,
                    )
                    await asyncio.sleep(wait_time)
                continue

            except (VestaAuthenticationError, VestaApiError):
                raise

            except httpx.HTTPError as err:
                last_error = VestaConnectionError(
                    f"HTTP error for {self._host}: {err}"
                )
                if attempt < retry_count:
                    wait_time = 2**attempt
                    await asyncio.sleep(wait_time)
                continue

        if last_error:
            raise last_error

        raise VestaConnectionError(f"Failed to connect to {self._host}")

    async def authenticate(self) -> bool:
        """Test authentication with the panel.

        Returns:
            True if authentication succeeds.

        Raises:
            VestaAuthenticationError: If authentication fails.
            VestaConnectionError: If connection fails.
        """
        _LOGGER.debug("Testing authentication with %s", self._host)
        try:
            # Use panelCondGet to test auth (login endpoint returns HTML)
            await self._request("GET", ENDPOINT_PANEL_STATUS)
            _LOGGER.debug("Authentication successful for %s", self._host)
            return True
        except VestaAuthenticationError:
            _LOGGER.error("Authentication failed for %s", self._host)
            raise

    async def get_panel_status(self) -> PanelStatus:
        """Get the current panel status.

        Returns:
            PanelStatus object with current mode and diagnostics.

        Raises:
            VestaConnectionError: If connection fails.
            VestaApiError: If parsing fails.
        """
        _LOGGER.debug("Fetching panel status from %s", self._host)
        json_data = await self._request("GET", ENDPOINT_PANEL_STATUS)

        try:
            updates = json_data.get("updates", json_data)
            return PanelStatus.model_validate(updates)
        except Exception as err:
            _LOGGER.error("Failed to parse panel status: %s", err)
            raise VestaApiError(f"Failed to parse panel status: {err}") from err

    async def get_devices(self) -> list[DeviceStatus]:
        """Get the list of all devices/zones.

        Returns:
            List of DeviceStatus objects for each device.

        Raises:
            VestaConnectionError: If connection fails.
            VestaApiError: If parsing fails.
        """
        _LOGGER.debug("Fetching device list from %s", self._host)
        json_data = await self._request("GET", ENDPOINT_DEVICE_LIST)

        try:
            devices = []
            for device_data in json_data.get("senrows", []):
                try:
                    devices.append(DeviceStatus.model_validate(device_data))
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to parse device %s: %s",
                        device_data.get("id", "unknown"),
                        err,
                    )
            return devices
        except Exception as err:
            _LOGGER.error("Failed to parse device list: %s", err)
            raise VestaApiError(f"Failed to parse device list: {err}") from err

    async def get_all_data(self) -> VestaData:
        """Get all data from the panel in a single call.

        This method fetches panel status, device list, event log, and reported
        events concurrently. Event log and reported events failures are
        non-critical and degrade gracefully to empty lists.

        Returns:
            VestaData object containing panel status, devices, event log,
            and reported events.
        """
        _LOGGER.debug("Fetching all data from %s", self._host)
        panel_task = self.get_panel_status()
        devices_task = self.get_devices()
        event_log_task = self.get_event_log()
        reported_events_task = self.get_reported_events()

        panel, devices, event_log_result, reported_events_result = await asyncio.gather(
            panel_task, devices_task, event_log_task, reported_events_task,
            return_exceptions=True
        )

        # Panel and devices are critical -- re-raise their exceptions
        if isinstance(panel, BaseException):
            raise panel
        if isinstance(devices, BaseException):
            raise devices

        # Event log is non-critical -- degrade gracefully
        if isinstance(event_log_result, BaseException):
            _LOGGER.warning("Failed to fetch event log: %s", event_log_result)
            event_log_result = []

        # Reported events are non-critical -- degrade gracefully
        if isinstance(reported_events_result, BaseException):
            _LOGGER.warning("Failed to fetch reported events: %s", reported_events_result)
            reported_events_result = []

        return VestaData(
            panel=panel,
            devices=devices,
            event_log=event_log_result,
            reported_events=reported_events_result,
        )

    async def get_event_log(self, max_count: int = 1000) -> list[EventLogEntry]:
        """Get the event log from the panel.

        Args:
            max_count: Maximum number of log entries to request. Default is 20.

        Returns:
            List of EventLogEntry objects, sorted by most recent first.

        Raises:
            VestaConnectionError: If connection fails.
            VestaApiError: If parsing fails.
        """
        _LOGGER.debug("Fetching event log from %s", self._host)
        json_data = await self._request(
            "POST", ENDPOINT_EVENT_LOG, data={"max_count": str(max_count)}
        )

        try:
            events = []
            for event_data in json_data.get("logrows", []):
                try:
                    events.append(EventLogEntry.model_validate(event_data))
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to parse event log entry: %s",
                        err,
                    )
            return events
        except Exception as err:
            _LOGGER.error("Failed to parse event log: %s", err)
            raise VestaApiError(f"Failed to parse event log: {err}") from err

    async def get_reported_events(self, max_count: int = 200) -> list[ReportedEvent]:
        """Get reported events from the panel for triggered state detection.

        Args:
            max_count: Maximum number of events to request. Default is 200.

        Returns:
            List of ReportedEvent objects, sorted by uid (highest = most recent).

        Raises:
            VestaConnectionError: If connection fails.
            VestaApiError: If parsing fails.
        """
        _LOGGER.debug("Fetching reported events from %s", self._host)
        json_data = await self._request(
            "POST", ENDPOINT_REPORTED_EVENTS, data={"max_count": str(max_count)}
        )

        try:
            events = []
            for event_data in json_data.get("rptrows", []):
                try:
                    events.append(ReportedEvent.model_validate(event_data))
                except Exception as err:
                    _LOGGER.warning(
                        "Failed to parse reported event: %s",
                        err,
                    )
            # Sort by uid descending (highest uid = most recent)
            events.sort(key=lambda e: e.uid, reverse=True)
            return events
        except Exception as err:
            _LOGGER.error("Failed to parse reported events: %s", err)
            raise VestaApiError(f"Failed to parse reported events: {err}") from err

    async def get_captures(self, max_count: int = 10) -> list[CaptureEvent]:
        """Get the camera captures stored on the panel.

        Args:
            max_count: Maximum number of captures to request. The panel web
                page offers 10, 20, 50, 80 and 100.

        Returns:
            List of CaptureEvent objects as returned by the panel (newest
            first on the panels seen so far).

        Raises:
            VestaConnectionError: If connection fails.
            VestaApiError: If parsing fails.
        """
        _LOGGER.debug("Fetching captures from %s", self._host)
        json_data = await self._request(
            "POST", ENDPOINT_CAPTURE_LIST, data={"max_count": str(max_count)}
        )
        if not isinstance(json_data, dict):
            raise VestaApiError(f"Unexpected capture list: {json_data!r:.200}")

        captures = []
        for row in json_data.get("caprows", []) or []:
            try:
                captures.append(CaptureEvent.model_validate(row))
            except Exception as err:
                _LOGGER.warning("Failed to parse capture: %s", err)
        return captures

    async def download_file(self, path: str, retry_count: int = 2) -> bytes:
        """Download a file (e.g. a capture photo) from the panel.

        Uses the same HTTP client and Basic authentication as the API calls.

        Args:
            path: Absolute panel path, e.g. "/temp/download/x_001.jpg".
            retry_count: Number of retries on transient failures.

        Returns:
            The file content.

        Raises:
            ValueError: If the path is not a plain absolute panel path.
            VestaAuthenticationError: If authentication fails (401).
            VestaConnectionError: If connection fails.
            VestaApiError: If the panel does not return the file.
        """
        if not path.startswith("/") or "://" in path or ".." in path:
            raise ValueError(f"Refusing to download unexpected path: {path!r}")

        url = f"{self._root_url}{path}"
        client = await self._get_client()
        last_error: Exception | None = None

        for attempt in range(retry_count + 1):
            try:
                response = await client.get(url, headers={"Accept": "image/*,*/*"})
            except (httpx.TimeoutException, httpx.HTTPError) as err:
                last_error = VestaConnectionError(f"Download of {path} failed: {err}")
                if attempt < retry_count:
                    await asyncio.sleep(2**attempt)
                continue

            if response.status_code == 401:
                raise VestaAuthenticationError(f"Authentication failed for {self._host}")
            if response.status_code >= 400:
                raise VestaApiError(f"Download of {path} failed: HTTP {response.status_code}")

            content_type = response.headers.get("content-type", "")
            if content_type.startswith("image/") or (
                content_type == "" and response.content[:2] == b"\xff\xd8"
            ):
                return response.content

            if _is_session_expired(response.text) and attempt < retry_count:
                last_error = VestaApiError("Session expired")
                continue
            raise VestaApiError(
                f"Download of {path} returned '{content_type}': {response.text[:100]}"
            )

        raise last_error or VestaConnectionError(f"Download of {path} failed")

    async def set_alarm_mode(self, mode: int, area: int = 1) -> bool:
        """Set the alarm mode.

        Args:
            mode: The mode to set (0=Disarm, 1=Arm Away, 2=Arm Home, 3=Arm Night).
            area: The area number to set the mode for. Default is 1.

        Returns:
            True if the mode was set successfully.

        Raises:
            VestaConnectionError: If connection fails.
            VestaApiError: If the command fails.
        """
        _LOGGER.info(
            "Setting alarm mode to %d for area %d on %s",
            mode,
            area,
            self._host,
        )
        payload = {"area": str(area), "mode": str(mode)}
        response = await self._request("POST", ENDPOINT_PANEL_SET, data=payload)

        result = response.get("result")
        if result == 1 or result == "1":
            _LOGGER.debug("Alarm mode set successfully")
            return True

        _LOGGER.error("Failed to set alarm mode: %s", response)
        return False

    async def close(self) -> None:
        """Close the client session."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
            _LOGGER.debug("Client session closed")
