from __future__ import annotations

import asyncio
import os
import subprocess
import sys
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
class ServiceDefinition:
    service_id: str
    display_name: str
    description: str
    category: str
    command: tuple[str, ...]
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
        if not self.command or any(not item for item in self.command):
            raise ValueError("service command must not be empty")
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
    started_at: datetime | None = None
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
    status: ManagedServiceStatus = ManagedServiceStatus.STOPPED
    started_at: datetime | None = None
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

    async def start_service(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        record = self._record(service_id)
        async with record.lifecycle_lock:
            return await self._start_service_locked(service_id, record)

    async def _start_service_locked(
        self, service_id: str, record: _ManagedProcess
    ) -> ServiceSnapshot:
        definition = record.definition
        if not definition.controllable:
            raise ServiceConflict(
                "service.not_controllable",
                f"service {service_id} is not controllable",
            )
        async with self._lock:
            await self._refresh_locked(record)
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
            record.status = ManagedServiceStatus.STARTING
            record.last_error = None
        try:
            process = await self._spawn(definition)
            async with self._lock:
                record.process = process
                record.started_at = datetime.now(UTC)
                record.readers = {
                    asyncio.create_task(self._read_output(record, process.stdout, "stdout")),
                    asyncio.create_task(self._read_output(record, process.stderr, "stderr")),
                }
                record.watcher = asyncio.create_task(self._watch(record, process))
            await self._wait_ready(record)
            async with self._lock:
                if record.status is ManagedServiceStatus.STARTING:
                    record.status = ManagedServiceStatus.RUNNING
                return _snapshot(record)
        except Exception as exc:
            await self._mark_failed(record, str(exc))
            await self._terminate(record)
            async with self._lock:
                record.process = None
                record.started_at = None
                record.watcher = None
                record.readers.clear()
            raise ServiceManagerError(
                "service.start_failed",
                f"service {service_id} failed to start: {exc}",
            ) from exc

    async def stop(self, service_id: str) -> ServiceSnapshot:
        self._require_started()
        record = self._record(service_id)
        async with record.lifecycle_lock:
            return await self._stop_locked(record)

    async def _stop_locked(self, record: _ManagedProcess) -> ServiceSnapshot:
        async with self._lock:
            await self._refresh_locked(record)
            if record.process is None or record.status is ManagedServiceStatus.STOPPED:
                record.status = ManagedServiceStatus.STOPPED
                return _snapshot(record)
            if record.status is ManagedServiceStatus.STOPPING:
                return _snapshot(record)
            record.status = ManagedServiceStatus.STOPPING
        await self._terminate(record)
        async with self._lock:
            record.process = None
            record.status = ManagedServiceStatus.STOPPED
            record.started_at = None
            record.watcher = None
            record.readers.clear()
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
        )

    async def _wait_ready(self, record: _ManagedProcess) -> None:
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
            except (OSError, URLError) as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError(
                        f"health check did not become ready: {definition.health_url}"
                    ) from exc
                await asyncio.sleep(0.15)

    async def _terminate(self, record: _ManagedProcess) -> None:
        process = record.process
        if process is None:
            return
        timeout = record.definition.shutdown_timeout_seconds
        if sys.platform == "win32":
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(taskkill.wait(), timeout=timeout)
            except TimeoutError:
                taskkill.kill()
        else:
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
        for reader in tuple(record.readers):
            if not reader.done():
                reader.cancel()
        if record.readers:
            await asyncio.gather(*record.readers, return_exceptions=True)

    async def _watch(
        self,
        record: _ManagedProcess,
        process: asyncio.subprocess.Process,
    ) -> None:
        try:
            return_code = await process.wait()
            async with self._lock:
                if record.process is process and record.status not in {
                    ManagedServiceStatus.STOPPING,
                    ManagedServiceStatus.STOPPED,
                }:
                    record.status = (
                        ManagedServiceStatus.STOPPED
                        if return_code == 0
                        else ManagedServiceStatus.FAILED
                    )
                    if return_code != 0:
                        record.last_error = f"process exited with code {return_code}"
                    record.process = None
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
            for record in records:
                await self._refresh_locked(record)

    async def _refresh(self, record: _ManagedProcess) -> None:
        async with self._lock:
            await self._refresh_locked(record)

    async def _refresh_locked(self, record: _ManagedProcess) -> None:
        process = record.process
        if process is not None and process.returncode is not None:
            record.status = (
                ManagedServiceStatus.STOPPED
                if process.returncode == 0
                else ManagedServiceStatus.FAILED
            )
            if process.returncode != 0:
                record.last_error = f"process exited with code {process.returncode}"
            record.process = None

    async def _mark_failed(self, record: _ManagedProcess, message: str) -> None:
        async with self._lock:
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
    return ServiceSnapshot(
        service_id=record.definition.service_id,
        display_name=record.definition.display_name,
        description=record.definition.description,
        category=record.definition.category,
        status=record.status,
        controllable=record.definition.controllable,
        endpoint=record.definition.endpoint,
        pid=record.process.pid if record.process is not None else None,
        started_at=record.started_at,
        last_error=record.last_error,
        recent_output=tuple(record.output),
    )


def _probe_health(url: str) -> None:
    request = Request(url, headers={"accept": "application/json"})
    with urlopen(request, timeout=0.75) as response:
        if response.status < 200 or response.status >= 300:
            raise OSError(f"health endpoint returned HTTP {response.status}")
