from __future__ import annotations

import asyncio
import contextlib
import ctypes
import math
import os
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from urllib.error import URLError
from urllib.request import Request, urlopen


class ManagedServiceStatus(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """PID plus the observed creation timestamp for one process generation."""

    pid: int
    create_time: float

    def __post_init__(self) -> None:
        if self.pid <= 0:
            raise ValueError("process pid must be positive")
        if not math.isfinite(self.create_time) or self.create_time <= 0:
            raise ValueError("process create time must be positive and finite")


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    service_id: str
    display_name: str
    description: str
    category: str
    command: tuple[str, ...] = ()
    working_directory: str | None = None
    endpoint: str | None = None
    health_url: str | None = None
    controllable: bool = True
    startup_timeout_seconds: float = 15.0
    shutdown_timeout_seconds: float = 8.0

    def __post_init__(self) -> None:
        for name, value in {
            "service_id": self.service_id,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category,
        }.items():
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.controllable and (not self.command or any(not item for item in self.command)):
            raise ValueError("controllable service command must not be empty")
        if self.startup_timeout_seconds <= 0 or self.shutdown_timeout_seconds <= 0:
            raise ValueError("service timeouts must be positive")
        if not self.controllable and self.command:
            raise ValueError("non-controllable services cannot have a managed command")


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    service_id: str
    display_name: str
    description: str
    category: str
    status: ManagedServiceStatus
    controllable: bool
    endpoint: str | None = None
    pid: int | None = None
    process_identity: ProcessIdentity | None = None
    epoch: int = 0
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    exit_code: int | None = None
    last_error: str | None = None
    recent_output: tuple[str, ...] = ()


class ServiceManagerError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ServiceNotFound(ServiceManagerError):
    pass


class ServiceConflict(ServiceManagerError):
    pass


@dataclass(slots=True)
class _ManagedProcess:
    definition: ServiceDefinition
    process: asyncio.subprocess.Process | None = None
    process_identity: ProcessIdentity | None = None
    epoch: int = 0
    status: ManagedServiceStatus = ManagedServiceStatus.STOPPED
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    exit_code: int | None = None
    last_error: str | None = None
    output: deque[str] = field(default_factory=lambda: deque(maxlen=40))
    readers: set[asyncio.Task[None]] = field(default_factory=set)
    watcher: asyncio.Task[None] | None = None
    lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class ServiceManager:
    """Static service catalog with bounded local subprocess lifecycle control."""

    def __init__(self, definitions: Iterable[ServiceDefinition]) -> None:
        values = tuple(definitions)
        ids = [definition.service_id for definition in values]
        if len(ids) != len(set(ids)):
            raise ValueError("service ids must be unique")
        self._records = {
            definition.service_id: _ManagedProcess(definition) for definition in values
        }
        self._lock = asyncio.Lock()
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        async with self._lock:
            if self._started:
                return
            self._started = True

    async def close(self) -> None:
        async with self._lock:
            if not self._started:
                return
            service_ids = tuple(self._records)
        await asyncio.gather(
            *(self.stop(service_id) for service_id in service_ids),
            return_exceptions=True,
        )
        async with self._lock:
            self._started = False

    async def list(self) -> tuple[ServiceSnapshot, ...]:
        self._require_started()
        await self._refresh_all()
        async with self._lock:
            return tuple(_snapshot(record) for record in self._records.values())

    async def get(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        record = self._record(service_id)
        await self._refresh(record)
        async with self._lock:
            return _snapshot(record)

    async def start_service(
        self, service_id: str, *, expected_epoch: int | None = None
    ) -> ServiceSnapshot:
        self._require_started()
        record = self._record(service_id)
        async with record.lifecycle_lock:
            return await self._start_service_locked(service_id, record, expected_epoch)

    async def _start_service_locked(
        self,
        service_id: str,
        record: _ManagedProcess,
        expected_epoch: int | None,
    ) -> ServiceSnapshot:
        definition = record.definition
        if not definition.controllable:
            raise ServiceConflict(
                "service.not_controllable",
                f"service {service_id} is not controllable",
            )
        await self._refresh(record)
        async with self._lock:
            _check_expected_epoch(record, expected_epoch)
            if record.status is ManagedServiceStatus.RUNNING:
                return _snapshot(record)
            if record.status in {
                ManagedServiceStatus.STARTING,
                ManagedServiceStatus.STOPPING,
            }:
                raise ServiceConflict(
                    "service.lifecycle_conflict",
                    f"service {service_id} is currently {record.status.value}",
                )
            record.epoch += 1
            record.status = ManagedServiceStatus.STARTING
            record.last_error = None
            record.exit_code = None
            record.stopped_at = None
            record.output.clear()
            generation = record.epoch
        try:
            process = await self._spawn(definition)
            identity = ProcessIdentity(process.pid, _process_create_time(process.pid))
            async with self._lock:
                record.process = process
                record.process_identity = identity
                record.started_at = datetime.now(UTC)
                record.readers = {
                    asyncio.create_task(self._read_output(record, process.stdout, "stdout")),
                    asyncio.create_task(self._read_output(record, process.stderr, "stderr")),
                }
                record.watcher = asyncio.create_task(
                    self._watch(record, process, generation, identity)
                )
            await self._wait_ready(record, generation)
            async with self._lock:
                if record.epoch != generation or record.process is not process:
                    raise ServiceConflict(
                        "service.generation_changed",
                        f"service {service_id} was replaced while starting",
                    )
                if record.status is not ManagedServiceStatus.STARTING:
                    raise RuntimeError(
                        f"service process exited before becoming running: {record.status.value}"
                    )
                record.status = ManagedServiceStatus.RUNNING
                return _snapshot(record)
        except Exception as exc:
            await self._mark_failed(record, str(exc), generation)
            await self._terminate(record, generation)
            watcher: asyncio.Task[None] | None
            async with self._lock:
                if record.epoch == generation:
                    record.process = None
                    record.process_identity = None
                    record.started_at = None
                    watcher = record.watcher
                    record.watcher = None
                    record.readers.clear()
                else:
                    watcher = None
            if watcher is not None:
                await asyncio.gather(watcher, return_exceptions=True)
            if isinstance(exc, ServiceConflict):
                raise
            raise ServiceManagerError(
                "service.start_failed",
                f"service {service_id} failed to start: {exc}",
            ) from exc

    async def stop(self, service_id: str, *, expected_epoch: int | None = None) -> ServiceSnapshot:
        self._require_started()
        record = self._record(service_id)
        async with record.lifecycle_lock:
            return await self._stop_locked(record, expected_epoch)

    async def _stop_locked(
        self, record: _ManagedProcess, expected_epoch: int | None
    ) -> ServiceSnapshot:
        await self._refresh(record)
        async with self._lock:
            _check_expected_epoch(record, expected_epoch)
            if record.process is None or record.status is ManagedServiceStatus.STOPPED:
                record.status = ManagedServiceStatus.STOPPED
                return _snapshot(record)
            if record.status is ManagedServiceStatus.STOPPING:
                return _snapshot(record)
            record.status = ManagedServiceStatus.STOPPING
            generation = record.epoch
        await self._terminate(record, generation)
        watcher: asyncio.Task[None] | None
        async with self._lock:
            if record.epoch == generation:
                record.process = None
                record.process_identity = None
                record.status = ManagedServiceStatus.STOPPED
                record.stopped_at = datetime.now(UTC)
                record.started_at = None
                watcher = record.watcher
                record.watcher = None
                record.readers.clear()
            else:
                watcher = None
        if watcher is not None and not watcher.done():
            await asyncio.gather(watcher, return_exceptions=True)
        async with self._lock:
            return _snapshot(record)

    async def _spawn(self, definition: ServiceDefinition) -> asyncio.subprocess.Process:
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        if sys.platform == "win32":
            return await asyncio.create_subprocess_exec(
                *definition.command,
                cwd=definition.working_directory,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        return await asyncio.create_subprocess_exec(
            *definition.command,
            cwd=definition.working_directory,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

    async def _wait_ready(self, record: _ManagedProcess, generation: int) -> None:
        definition = record.definition
        process = record.process
        if process is None:
            raise RuntimeError("service process was not attached")
        deadline = asyncio.get_running_loop().time() + definition.startup_timeout_seconds
        while True:
            if process.returncode is not None:
                output = "\n".join(record.output)
                raise RuntimeError(
                    f"process exited with code {process.returncode}: {output[-1000:]}"
                )
            async with self._lock:
                if record.epoch != generation or record.process is not process:
                    raise ServiceConflict(
                        "service.generation_changed",
                        "service generation changed during readiness check",
                    )
            if definition.health_url is None:
                await asyncio.sleep(0.15)
                if process.returncode is not None:
                    output = "\n".join(record.output)
                    raise RuntimeError(
                        f"process exited with code {process.returncode}: {output[-1000:]}"
                    )
                return
            try:
                await asyncio.to_thread(_probe_health, definition.health_url)
                return
            except (OSError, URLError, ValueError) as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"health check did not become ready: {definition.health_url}"
                    ) from exc
                await asyncio.sleep(0.15)

    async def _terminate(self, record: _ManagedProcess, generation: int) -> None:
        async with self._lock:
            if record.epoch != generation:
                return
            process = record.process
            timeout = record.definition.shutdown_timeout_seconds
            readers = tuple(record.readers)
        if process is None:
            return
        if sys.platform == "win32":
            await _terminate_windows_tree(process.pid, timeout)
            try:
                await asyncio.wait_for(process.wait(), timeout=max(timeout / 2, 0.2))
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        else:
            await _terminate_posix_tree(process, timeout)
        await _finish_readers(readers, timeout)

    async def _watch(
        self,
        record: _ManagedProcess,
        process: asyncio.subprocess.Process,
        generation: int,
        identity: ProcessIdentity,
    ) -> None:
        try:
            return_code = await process.wait()
            readers: tuple[asyncio.Task[None], ...] = ()
            async with self._lock:
                if (
                    record.epoch == generation
                    and record.process is process
                    and record.process_identity == identity
                ):
                    expected_stop = record.status in {
                        ManagedServiceStatus.STOPPING,
                        ManagedServiceStatus.STOPPED,
                    }
                    record.status = (
                        ManagedServiceStatus.STOPPED
                        if expected_stop or return_code == 0
                        else ManagedServiceStatus.FAILED
                    )
                    if return_code != 0 and not expected_stop:
                        record.last_error = f"process exited with code {return_code}"
                    record.exit_code = return_code
                    record.stopped_at = datetime.now(UTC)
                    record.process = None
                    record.process_identity = None
                    record.watcher = None
                    readers = tuple(record.readers)
                    record.readers.clear()
            await _finish_readers(readers, 1.0)
        except asyncio.CancelledError:
            raise

    async def _read_output(
        self,
        record: _ManagedProcess,
        stream: asyncio.StreamReader | None,
        channel: str,
    ) -> None:
        if stream is None:
            return
        try:
            async for line in stream:
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    async with self._lock:
                        record.output.append(f"[{channel}] {text}")
        except asyncio.CancelledError:
            raise

    async def _refresh_all(self) -> None:
        async with self._lock:
            records = tuple(self._records.values())
        await asyncio.gather(*(self._refresh(record) for record in records))

    async def _refresh(self, record: _ManagedProcess) -> None:
        async with self._lock:
            watcher = await self._refresh_locked(record)
        if watcher is not None and not watcher.done():
            await asyncio.gather(watcher, return_exceptions=True)

    async def _refresh_locked(self, record: _ManagedProcess) -> asyncio.Task[None] | None:
        process = record.process
        if process is None or process.returncode is None:
            return None
        return_code = process.returncode
        expected_stop = record.status in {
            ManagedServiceStatus.STOPPING,
            ManagedServiceStatus.STOPPED,
        }
        record.status = (
            ManagedServiceStatus.STOPPED
            if expected_stop or return_code == 0
            else ManagedServiceStatus.FAILED
        )
        if return_code != 0 and not expected_stop:
            record.last_error = f"process exited with code {return_code}"
        record.exit_code = return_code
        record.stopped_at = datetime.now(UTC)
        watcher = record.watcher
        if watcher is None or watcher.done():
            record.process = None
            record.process_identity = None
            if watcher is not None:
                record.watcher = None
            record.readers.clear()
        return watcher

    async def _mark_failed(self, record: _ManagedProcess, message: str, generation: int) -> None:
        async with self._lock:
            if record.epoch == generation:
                record.status = ManagedServiceStatus.FAILED
                record.last_error = message

    def _record(self, service_id: str) -> _ManagedProcess:
        try:
            return self._records[service_id]
        except KeyError as exc:
            raise ServiceNotFound(
                "service.not_found",
                f"service {service_id} is not registered",
            ) from exc

    def _require_started(self) -> None:
        if not self._started:
            raise ServiceManagerError(
                "service_manager.not_started",
                "service manager is not started",
            )


def _snapshot(record: _ManagedProcess) -> ServiceSnapshot:
    identity = record.process_identity
    return ServiceSnapshot(
        service_id=record.definition.service_id,
        display_name=record.definition.display_name,
        description=record.definition.description,
        category=record.definition.category,
        status=record.status,
        controllable=record.definition.controllable,
        endpoint=record.definition.endpoint,
        pid=identity.pid if identity is not None else None,
        process_identity=identity,
        epoch=record.epoch,
        started_at=record.started_at,
        stopped_at=record.stopped_at,
        exit_code=record.exit_code,
        last_error=record.last_error,
        recent_output=tuple(record.output),
    )


def _check_expected_epoch(record: _ManagedProcess, expected_epoch: int | None) -> None:
    if expected_epoch is not None and expected_epoch != record.epoch:
        raise ServiceConflict(
            "service.epoch_fenced",
            f"service epoch {expected_epoch} is stale; current epoch is {record.epoch}",
        )


def _process_create_time(pid: int) -> float:
    if sys.platform == "win32":
        value = _windows_process_create_time(pid)
        if value is not None:
            return value
    return time.time()


def _windows_process_create_time(pid: int) -> float | None:
    process_query_limited_information = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        get_times = ctypes.windll.kernel32.GetProcessTimes
        get_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        get_times.restype = ctypes.c_bool
        if not get_times(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        return creation.value / 10_000_000 - 11_644_473_600
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


async def _terminate_windows_tree(pid: int, limit: float) -> None:
    async def run(force: bool) -> int:
        args = ["taskkill.exe", "/PID", str(pid), "/T"]
        if force:
            args.append("/F")
        taskkill = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return await asyncio.wait_for(taskkill.wait(), timeout=max(limit / 2, 0.2))
        except TimeoutError:
            taskkill.kill()
            await taskkill.wait()
            return -1

    await run(False)
    await run(True)


async def _terminate_posix_tree(process: asyncio.subprocess.Process, limit: float) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=limit)
        return
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _finish_readers(readers: tuple[asyncio.Task[None], ...], limit: float) -> None:
    if not readers:
        return
    pending = tuple(reader for reader in readers if not reader.done())
    if pending:
        try:
            async with asyncio.timeout(max(limit, 0.2)):
                await asyncio.gather(*pending, return_exceptions=True)
        except TimeoutError:
            for reader in pending:
                if not reader.done():
                    reader.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    else:
        await asyncio.gather(*readers, return_exceptions=True)


def _probe_health(url: str) -> None:
    request = Request(url, headers={"accept": "application/json"})
    with urlopen(request, timeout=0.75) as response:
        if response.status < 200 or response.status >= 300:
            raise OSError(f"health endpoint returned HTTP {response.status}")
        response.read(1)
