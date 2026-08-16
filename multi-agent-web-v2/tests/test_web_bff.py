from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

import httpx2
from fastapi import FastAPI
from pydantic import SecretStr

from multi_agent_web_v2.core_client import CoreClient, CoreResponse
from multi_agent_web_v2.main import (
    _persistent_sse,  # pyright: ignore[reportPrivateUsage]
    _token_sse,  # pyright: ignore[reportPrivateUsage]
    create_internal_app,
    create_public_app,
)
from multi_agent_web_v2.settings import WebSettings
from multi_agent_web_v2.stream_hub import StreamHub, TokenEvent


class _FakeCore:
    def __init__(self, *, events: list[dict[str, object]] | None = None) -> None:
        self.requests: list[tuple[str, str, Mapping[str, str], bytes | None]] = []
        self.events = events or []
        self.event_queries: list[tuple[tuple[str, str], ...]] = []

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
        headers: Mapping[str, str] | None = None,
        content: bytes | None = None,
    ) -> CoreResponse:
        del params
        self.requests.append((method, path, dict(headers or {}), content))
        if path == "/ready":
            return CoreResponse(200, b'{"status":"ready"}', "application/json")
        return CoreResponse(202, b'{"accepted":true}', "application/json")

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        params: tuple[tuple[str, str], ...] = (),
    ) -> object:
        assert (method, path) == ("GET", "/api/v2/events")
        self.event_queries.append(params)
        after = int(dict(params).get("after", "0"))
        return [event for event in self.events if cast(int, event["deliveryId"]) > after]


def _settings(tmp_path: Path, **updates: object) -> WebSettings:
    values: dict[str, object] = {
        "frontend_dist": tmp_path / "dist",
        "maximum_proxy_body_bytes": 64,
    }
    values.update(updates)
    return WebSettings.model_validate(values)


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncGenerator[httpx2.AsyncClient]:
    transport = httpx2.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_public_bff_proxies_only_selected_headers_and_removes_origin(
    tmp_path: Path,
) -> None:
    core = _FakeCore()
    app = create_public_app(
        _settings(tmp_path),
        core=cast(CoreClient, core),
    )

    async with _client(app) as client:
        response = await client.post(
            "/api/v2/templates",
            headers={
                "Origin": "http://testserver",
                "Idempotency-Key": "template-1",
                "Authorization": "Bearer must-not-forward",
                "Content-Type": "application/json",
            },
            content=b'{"name":"Example"}',
        )

    assert response.status_code == 202
    method, path, headers, body = core.requests[0]
    assert (method, path, body) == ("POST", "/api/v2/templates", b'{"name":"Example"}')
    assert headers["idempotency-key"] == "template-1"
    assert "authorization" not in headers
    assert "origin" not in headers


async def test_public_bff_rejects_cross_origin_and_oversized_writes(tmp_path: Path) -> None:
    core = _FakeCore()
    app = create_public_app(
        _settings(tmp_path, maximum_proxy_body_bytes=8),
        core=cast(CoreClient, core),
    )

    async with _client(app) as client:
        cross_origin = await client.post(
            "/api/v2/templates",
            headers={"Origin": "https://evil.example"},
            content=b"{}",
        )
        oversized = await client.post(
            "/api/v2/templates",
            content=b"x" * 9,
        )

    assert cross_origin.status_code == 403
    assert oversized.status_code == 413
    assert core.requests == []


async def test_public_bff_serves_spa_and_security_headers(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<html>console</html>", encoding="utf-8")
    app = create_public_app(
        _settings(tmp_path),
        core=cast(CoreClient, _FakeCore()),
    )

    async with _client(app) as client:
        response = await client.get("/instances/example")

    assert response.status_code == 200
    assert response.headers["x-frame-options"] == "DENY"
    assert "console" in response.text


async def test_internal_ingress_requires_token_and_publishes_to_shared_hub(
    tmp_path: Path,
) -> None:
    settings = _settings(
        tmp_path,
        internal_stream_token=SecretStr("stream-secret"),
    )
    hub = StreamHub(queue_size=8)
    app = create_internal_app(settings, hub=hub)

    async with hub.subscribe() as queue, _client(app) as client:
        rejected = await client.post(
            "/internal/v1/token-batches",
            json={
                "events": [
                    {
                        "executionId": "execution-1",
                        "sequence": 1,
                        "kind": "text_delta",
                        "text": "hello",
                    }
                ]
            },
        )
        accepted = await client.post(
            "/internal/v1/token-batches",
            headers={"X-Misaka-Stream-Token": "stream-secret"},
            json={
                "events": [
                    {
                        "executionId": "execution-1",
                        "sequence": 1,
                        "kind": "text_delta",
                        "text": "hello",
                    }
                ]
            },
        )

        event = await queue.get()

    assert rejected.status_code == 401
    assert accepted.json() == {"accepted": 1}
    assert event.text == "hello"


async def test_stream_uses_last_event_id_as_the_persistent_cursor(tmp_path: Path) -> None:
    captured: dict[str, int] = {}

    async def one_message(**kwargs: object) -> AsyncGenerator[bytes]:
        captured["cursor"] = cast(int, kwargs["cursor"])
        yield b"event: complete\ndata: {}\n\n"

    app = create_public_app(
        _settings(tmp_path),
        core=cast(CoreClient, _FakeCore()),
    )
    with patch("multi_agent_web_v2.main._event_stream", one_message):
        async with _client(app) as client:
            response = await client.get(
                "/api/v2/stream?after=3",
                headers={"Last-Event-ID": "8"},
            )

    assert response.status_code == 200
    assert captured == {"cursor": 8}


async def test_persistent_milestones_replay_after_a_bff_restart() -> None:
    events: list[dict[str, object]] = [
        {"deliveryId": 10, "eventType": "workflow.started"},
        {"deliveryId": 11, "eventType": "node.completed"},
    ]

    first_core = _FakeCore(events=events)
    first_cursor, first_messages = await _persistent_sse(
        cast(CoreClient, first_core),
        cursor=0,
    )
    restarted_core = _FakeCore(events=events)
    resumed_cursor, resumed_messages = await _persistent_sse(
        cast(CoreClient, restarted_core),
        cursor=10,
    )

    assert first_cursor == 11
    assert [message.splitlines()[0] for message in first_messages] == [b"id: 10", b"id: 11"]
    assert resumed_cursor == 11
    assert len(resumed_messages) == 1
    assert resumed_messages[0].startswith(b"id: 11\nevent: milestone\n")
    assert restarted_core.event_queries == [(("after", "10"), ("limit", "1000"))]


def test_token_events_are_ephemeral_and_do_not_advance_the_sse_cursor() -> None:
    message = _token_sse(
        TokenEvent(
            execution_id="execution-1",
            sequence=4,
            kind="text_delta",
            text="hello",
        )
    )

    assert message.startswith(b"event: token\ndata: ")
    assert b"\nid:" not in message


def test_web_settings_keep_control_and_internal_listeners_on_loopback() -> None:
    for values in (
        {"control_api_url": "http://192.168.1.10:8011"},
        {"internal_host": "0.0.0.0"},
    ):
        try:
            WebSettings.model_validate(values)
        except ValueError:
            continue
        raise AssertionError(f"settings unexpectedly accepted {values}")
