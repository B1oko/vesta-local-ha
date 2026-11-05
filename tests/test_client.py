"""Tests for the HTTP client: capture list, photo download and retries."""
from __future__ import annotations

import base64

import httpx
import pytest

from custom_components.vesta_local.client import (
    VestaApiError,
    VestaAuthenticationError,
    VestaLocalClient,
)

from .conftest import load_json, photo_bytes


def make_client(handler) -> tuple[VestaLocalClient, list[httpx.Request]]:
    """Return a client whose HTTP traffic goes to ``handler``."""
    requests: list[httpx.Request] = []

    def recorder(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    client = VestaLocalClient("192.168.1.184", "user", "secret")
    client._client = httpx.AsyncClient(
        auth=httpx.BasicAuth("user", "secret"),
        headers=client._headers,
        transport=httpx.MockTransport(recorder),
    )
    return client, requests


async def test_get_captures_parses_panel_answer() -> None:
    """The real captureEventListGet answer is parsed."""
    client, requests = make_client(
        lambda r: httpx.Response(200, json=load_json("captureEventListGet.json"))
    )
    captures = await client.get_captures(20)

    assert requests[0].method == "POST"
    assert requests[0].url.path == "/action/captureEventListGet"
    assert requests[0].content == b"max_count=20"
    assert len(captures) == 1
    capture = captures[0]
    assert capture.device_id == "IN:032a4e00"
    assert capture.zone == "3"
    assert capture.area == "1"
    assert capture.status == "Done"
    assert capture.type == "Alarm"
    assert capture.time == "2026-11-05 14:51:23"
    assert len(capture.image_files) == 6
    assert capture.image_files[0] == "/temp/download/032a4e00_2026-11-05_145113_001.jpg"
    assert capture.key == "IN:032a4e00|2026-11-05 14:51:23|032a4e00_2026-11-05_145113_001.jpg"


async def test_download_file_uses_basic_auth() -> None:
    """Photos are fetched from the panel root with the same credentials."""
    content = photo_bytes()
    client, requests = make_client(
        lambda r: httpx.Response(200, content=content, headers={"Content-Type": "image/jpeg"})
    )
    result = await client.download_file("/temp/download/a_001.jpg")

    assert result == content
    assert str(requests[0].url) == "http://192.168.1.184/temp/download/a_001.jpg"
    expected = "Basic " + base64.b64encode(b"user:secret").decode()
    assert requests[0].headers["Authorization"] == expected


async def test_session_expired_is_retried() -> None:
    """'Access Denied: Session expired!' is answered again on the next try."""
    answers = [
        httpx.Response(200, text="Access Denied: Session expired!"),
        httpx.Response(200, json=load_json("captureEventListGet.json")),
    ]
    client, requests = make_client(lambda r: answers.pop(0))

    captures = await client.get_captures()

    assert len(requests) == 2
    assert len(captures) == 1


async def test_download_session_expired_is_retried() -> None:
    """The retry also applies to photo downloads."""
    answers = [
        httpx.Response(200, text="Access Denied: Session expired!", headers={"Content-Type": "text/html"}),
        httpx.Response(200, content=photo_bytes(), headers={"Content-Type": "image/jpeg"}),
    ]
    client, requests = make_client(lambda r: answers.pop(0))

    assert await client.download_file("/temp/download/a_001.jpg") == photo_bytes()
    assert len(requests) == 2


async def test_download_errors() -> None:
    """Bad paths, 401 and non-image answers raise."""
    client, _ = make_client(lambda r: httpx.Response(401))
    with pytest.raises(VestaAuthenticationError):
        await client.download_file("/temp/download/a.jpg")

    client, _ = make_client(lambda r: httpx.Response(200, text="<html>nope</html>", headers={"Content-Type": "text/html"}))
    with pytest.raises(VestaApiError):
        await client.download_file("/temp/download/a.jpg")

    for bad in ("temp/a.jpg", "/temp/../etc/passwd", "http://evil/a.jpg"):
        with pytest.raises(ValueError):
            await client.download_file(bad)


async def test_device_type_and_camera_detection() -> None:
    """The numeric type is parsed and type 27 is a camera."""
    client, _ = make_client(lambda r: httpx.Response(200, json=load_json("deviceListGet.json")))
    devices = await client.get_devices()

    assert [d.type_id for d in devices] == [9, 9, 27, 17]
    assert [d.is_camera for d in devices] == [False, False, True, False]
