from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import psutil
import pytest

from multi_agent_v2.packages.process_runtime import (
    LocalManagedProcess,
    LocalProcessRuntime,
    OutputCaptureSpec,
    ProcessSpawnSpec,
    ProcessTerminationError,
    scrubbed_parent_environment,
)


def _capture(directory: Path, *, memory: int = 256, spill: int = 16_384) -> OutputCaptureSpec:
    return OutputCaptureSpec(
        disposition="collect",
        memory_limit_bytes=memory,
        spill_limit_bytes=spill,
        spill_directory=directory,
    )


def _spec(
    tmp_path: Path,
    code: str,
    *,
    env: dict[str, str | None] | None = None,
) -> ProcessSpawnSpec:
    return ProcessSpawnSpec(
        argv=(sys.executable, "-c", code),
        cwd=tmp_path,
        env=env or {},
        stdin="discard",
        stdout=_capture(tmp_path / "spill"),
        stderr=_capture(tmp_path / "spill"),
        grace_seconds=2,
    )


def test_scrubbed_parent_environment_removes_credential_shaped_names() -> None:
    environment = scrubbed_parent_environment(
        {
            "SAFE": "ok",
            "API_KEY": "secret",
            "accessToken": "secret",
            "PASSWORD_FILE": "secret",
            "NOT_SECRET": "also-secret",
        }
    )

    assert environment == {"SAFE": "ok"}


@pytest.mark.asyncio
async def test_process_runtime_collects_bounded_output_and_spills(tmp_path: Path) -> None:
    runtime = LocalProcessRuntime()
    process = await runtime.spawn(_spec(tmp_path, "print('x' * 4096)"))

    outcome = await process.done
    output = process.read_stdout()

    assert outcome.exit_code == 0
    assert outcome.tree_quiescent
    assert outcome.stdout_truncated
    assert output is not None
    assert output.lossy
    assert len(output.text.encode()) <= 256
    assert output.spill_path is not None
    assert len(output.spill_path.read_text(encoding="utf-8")) == 4097
    await runtime.aclose()


@pytest.mark.asyncio
async def test_process_runtime_uses_scrubbed_environment_with_explicit_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MULTI_AGENT_TEST_TOKEN", "ambient-secret")
    monkeypatch.setenv("MULTI_AGENT_SAFE", "ambient-safe")
    runtime = LocalProcessRuntime()
    process = await runtime.spawn(
        _spec(
            tmp_path,
            (
                "import os; "
                "print(os.environ.get('MULTI_AGENT_TEST_TOKEN', 'missing')); "
                "print(os.environ['MULTI_AGENT_SAFE']); "
                "print(os.environ['EXPLICIT_TOKEN'])"
            ),
            env={
                "MULTI_AGENT_SAFE": "overridden",
                "EXPLICIT_TOKEN": "explicitly-authorized",
            },
        )
    )

    await process.done
    output = process.read_stdout()

    assert output is not None
    assert output.text.splitlines() == [
        "missing",
        "overridden",
        "explicitly-authorized",
    ]
    await runtime.aclose()


@pytest.mark.asyncio
async def test_process_runtime_terminates_the_child_tree(tmp_path: Path) -> None:
    runtime = LocalProcessRuntime()
    try:
        process = await runtime.spawn(
            _spec(
                tmp_path,
                (
                    "import subprocess, sys, time; "
                    "child=subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)']); "
                    "print(child.pid, flush=True); "
                    "time.sleep(60)"
                ),
            )
        )

        child_pid = await _read_child_pid(process)
        child_create_time = psutil.Process(child_pid).create_time()
        outcome = await process.terminate()

        assert outcome.cancel_requested
        assert outcome.cancel_confirmed
        assert outcome.tree_quiescent
        assert not _same_process_exists(child_pid, child_create_time)
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_process_runtime_fails_closed_when_tree_quiescence_is_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unconfirmed(_: LocalManagedProcess) -> bool:
        return False

    monkeypatch.setattr(LocalManagedProcess, "_wait_for_tree_quiescence", unconfirmed)
    runtime = LocalProcessRuntime()
    process = await runtime.spawn(_spec(tmp_path, "import time; time.sleep(60)"))

    with pytest.raises(ProcessTerminationError, match="not confirmed"):
        await process.terminate()

    assert process.done.done()
    assert not (await process.done).tree_quiescent
    with pytest.raises(ProcessTerminationError, match="not confirmed"):
        await runtime.aclose()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object acceptance")
@pytest.mark.asyncio
async def test_normal_parent_exit_closes_the_windows_job_and_kills_descendants(
    tmp_path: Path,
) -> None:
    runtime = LocalProcessRuntime()
    try:
        process = await runtime.spawn(
            _spec(
                tmp_path,
                (
                    "import subprocess, sys; "
                    "child=subprocess.Popen([sys.executable, '-c', "
                    "'import time; time.sleep(60)']); "
                    "print(child.pid, flush=True)"
                ),
            )
        )

        assert await process.wait_for_exit(5)
        output = process.read_stdout()
        assert output is not None
        child_pid = int(output.text.strip().splitlines()[0])

        assert (await process.done).tree_quiescent
        assert not psutil.pid_exists(child_pid)
    finally:
        await runtime.aclose()


async def _read_child_pid(process: LocalManagedProcess) -> int:
    for _ in range(100):
        output = process.read_stdout()
        if output is not None and output.text.strip():
            return int(output.text.strip().splitlines()[0])
        await asyncio.sleep(0.05)
    raise AssertionError("child PID was not reported")


def _same_process_exists(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == create_time and process.is_running()
    except psutil.Error:
        return False
