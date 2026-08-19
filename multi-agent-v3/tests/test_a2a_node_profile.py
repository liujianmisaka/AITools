from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from a2a.types.a2a_pb2 import Task, TaskState
from misaka_a2a_capability import TaskRequest
from misaka_a2a_http import A2AHttpClient
from misaka_a2a_node import create_fake_a2a_node
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_invocation_contracts import CapabilityFeature
from misaka_kernel import HostStatus


def _request(task_id: str) -> TaskRequest:
    return TaskRequest(
        task_id=task_id,
        context_id="context-client",
        message_id=f"message-{task_id}",
        idempotency_key=f"idem-{task_id}",
        capability_id=AGENT_CAPABILITY_ID,
        operation=AGENT_OPERATION_INVOKE,
        input={"prompt": "Return the deterministic fake answer"},
        provider_id="fake-agent",
        model="fake/model",
        effort="high",
        required_features=frozenset({CapabilityFeature.STREAMING}),
        output_schema={
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    )


def test_wildcard_bind_requires_an_explicit_public_url() -> None:
    from misaka_a2a_node import A2ANodeConfig

    with pytest.raises(ValueError, match="public_url"):
        A2ANodeConfig(host="0.0.0.0")


@pytest.mark.asyncio
async def test_a2a_node_profile_starts_and_stops_kernel_and_server_together() -> None:
    node = create_fake_a2a_node()

    await node.start()
    result = await (await node.server.submit(_request("task-profile"))).wait()
    await node.stop()

    assert result.output == {"answer": "ok"}
    assert result.delegation_id is not None
    assert result.invocation_id is not None
    assert result.activation_id is not None
    assert result.delegation_id != result.invocation_id
    assert result.activation_id not in {result.delegation_id, result.invocation_id}
    assert node.host_status is HostStatus.STOPPED
    assert node.server.active_task_count == 0


def test_official_client_works_against_real_uvicorn_and_process_cleans_up() -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "misaka_a2a_node",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--public-url",
            base_url,
            "--log-level",
            "warning",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creation_flags,
    )
    try:
        _wait_healthy(base_url, process)
        task, sequences = asyncio.run(_exercise_official_client(base_url))

        assert task.id == "task-real-http"
        assert TaskState.Name(task.status.state) == "TASK_STATE_COMPLETED"
        assert "delegationId" in task.metadata.fields
        assert "invocationId" in task.metadata.fields
        assert "activationId" in task.metadata.fields
        assert sequences
        assert sequences[0] == 3
    finally:
        _stop_process(process)

    assert process.poll() is not None
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0


async def _exercise_official_client(base_url: str) -> tuple[Task, list[int]]:
    async with A2AHttpClient(base_url) as client:
        task = await client.send(_request("task-real-http"))
        sequences: list[int] = []
        async for response in client.subscribe(task.id, start_sequence=3):
            if response.HasField("status_update"):
                raw = response.status_update.metadata.fields.get("sequence")
                if raw is not None:
                    sequences.append(int(raw.number_value))
        queried = await client.get(task.id)
        assert queried.id == task.id
        return task, sequences


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_healthy(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    last_error = "service did not respond"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise AssertionError(f"A2A node exited early: {output}")
        try:
            response = httpx.get(f"{base_url}/health", timeout=0.5)
            if response.status_code == 200 and response.json().get("a2aServer") == "active":
                return
            last_error = response.text
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.05)
    raise AssertionError(f"A2A node did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
