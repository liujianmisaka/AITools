from __future__ import annotations

import asyncio
import contextlib
import ctypes
import os
import signal
import subprocess
import tempfile
from asyncio import Future, Task
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import BinaryIO, Final, cast

import psutil

from multi_agent_v2.packages.process_runtime.models import (
    OutputCaptureSpec,
    ProcessOutcome,
    ProcessOutputRead,
    ProcessSpawnSpec,
    ProcessTerminationError,
)

_SENSITIVE_ENV_PARTS: Final[tuple[str, ...]] = ("KEY", "SECRET", "TOKEN", "PASSWORD")
_READ_SIZE: Final[int] = 64 * 1024


def scrubbed_parent_environment(
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if parent is None else parent
    return {
        name: value
        for name, value in source.items()
        if not any(part in name.upper() for part in _SENSITIVE_ENV_PARTS)
    }


class _BoundedCollector:
    def __init__(self, spec: OutputCaptureSpec) -> None:
        self._spec = spec
        self._tail = bytearray()
        self._total_bytes = 0
        self._truncated = False
        self._spill_path: Path | None = None
        self._spill: BinaryIO | None = None
        self._spill_bytes = 0
        self._spill_complete = True

    @property
    def truncated(self) -> bool:
        return self._truncated

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._total_bytes += len(chunk)
        self._tail.extend(chunk)
        overflow = len(self._tail) - self._spec.memory_limit_bytes
        if overflow > 0:
            del self._tail[:overflow]
            self._truncated = True
        self._append_spill(chunk)

    def read_from(self, from_offset: int) -> ProcessOutputRead:
        if from_offset < 0 or from_offset > self._total_bytes:
            raise ValueError("output offset is outside the captured stream")
        tail_start = self._total_bytes - len(self._tail)
        lossy = from_offset < tail_start
        start = 0 if lossy else from_offset - tail_start
        return ProcessOutputRead(
            text=bytes(self._tail[start:]).decode("utf-8", errors="replace"),
            next_offset=self._total_bytes,
            lossy=lossy,
            spill_path=self._spill_path if self._spill_complete else None,
        )

    def close(self) -> None:
        if self._spill is not None:
            self._spill.flush()
            os.fsync(self._spill.fileno())
            self._spill.close()
            self._spill = None

    def _append_spill(self, chunk: bytes) -> None:
        directory = self._spec.spill_directory
        if directory is None or not self._spill_complete:
            return
        if self._spill_bytes + len(chunk) > self._spec.spill_limit_bytes:
            self._spill_complete = False
            self._discard_spill()
            return
        if self._spill is None:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix="process-",
                suffix=".log",
                dir=directory,
            )
            os.chmod(raw_path, 0o600)
            self._spill_path = Path(raw_path)
            self._spill = os.fdopen(descriptor, "wb")
        self._spill.write(chunk)
        self._spill_bytes += len(chunk)

    def _discard_spill(self) -> None:
        if self._spill is not None:
            self._spill.close()
            self._spill = None
        if self._spill_path is not None:
            self._spill_path.unlink(missing_ok=True)
            self._spill_path = None


class _WindowsJob:
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final[int] = 0x00002000
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: Final[int] = 9
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION: Final[int] = 1
    _JOB_OBJECT_BASIC_PROCESS_ID_LIST: Final[int] = 3
    _ERROR_MORE_DATA: Final[int] = 234

    def __init__(self, process_handle: int) -> None:
        self._handle: int | None = None
        self.error: str | None = None
        if os.name != "nt":
            self.error = "Windows Job Objects are unavailable on this platform"
            return
        try:
            self._create(process_handle)
        except OSError as exc:
            self.error = str(exc)
            with contextlib.suppress(OSError):
                self.close()

    @property
    def available(self) -> bool:
        return self._handle is not None

    def terminate(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.TerminateJobObject(self._handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def active_processes(self) -> int | None:
        if self._handle is None:
            return None

        class BasicAccounting(ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", ctypes.c_int64),
                ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64),
                ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32),
                ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32),
                ("TotalTerminatedProcesses", ctypes.c_uint32),
            ]

        info = BasicAccounting()
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        succeeded = kernel32.QueryInformationJobObject(
            self._handle,
            self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        )
        if not succeeded:
            raise ctypes.WinError(ctypes.get_last_error())
        return int(info.ActiveProcesses)

    def process_ids(self) -> tuple[int, ...]:
        if self._handle is None:
            return ()
        capacity = 16
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        while True:

            class ProcessIdList(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", ctypes.c_uint32),
                    ("NumberOfProcessIdsInList", ctypes.c_uint32),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            info = ProcessIdList()
            succeeded = kernel32.QueryInformationJobObject(
                self._handle,
                self._JOB_OBJECT_BASIC_PROCESS_ID_LIST,
                ctypes.byref(info),
                ctypes.sizeof(info),
                None,
            )
            if succeeded:
                return tuple(
                    int(info.ProcessIdList[index])
                    for index in range(int(info.NumberOfProcessIdsInList))
                )
            error = ctypes.get_last_error()
            if error != self._ERROR_MORE_DATA:
                raise ctypes.WinError(error)
            capacity = max(capacity * 2, int(info.NumberOfAssignedProcesses))

    def close(self) -> None:
        if self._handle is None:
            return
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = self._handle
        self._handle = None
        if not kernel32.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def _create(self, process_handle: int) -> None:
        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimitInformation),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._handle = int(handle)
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            self._handle,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not kernel32.AssignProcessToJobObject(self._handle, process_handle):
            raise ctypes.WinError(ctypes.get_last_error())


class LocalManagedProcess:
    def __init__(
        self,
        process: subprocess.Popen[bytes],
        *,
        spec: ProcessSpawnSpec,
        stdout_collector: _BoundedCollector | None,
        stderr_collector: _BoundedCollector | None,
        job: _WindowsJob | None,
    ) -> None:
        self._process = process
        self._spec = spec
        self._stdout_collector = stdout_collector
        self._stderr_collector = stderr_collector
        self._job = job
        self._cancel_requested = False
        self._timed_out = False
        self._termination_lock = asyncio.Lock()
        self._stdout_task = self._collector_task(
            cast(BinaryIO | None, process.stdout),
            stdout_collector,
        )
        self._stderr_task = self._collector_task(
            cast(BinaryIO | None, process.stderr),
            stderr_collector,
        )
        self._done_task: Task[ProcessOutcome] = asyncio.create_task(self._wait_for_outcome())
        try:
            self._create_time = psutil.Process(process.pid).create_time()
        except psutil.Error:
            self._create_time = 0.0

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def create_time(self) -> float:
        return self._create_time

    @property
    def stdin(self) -> BinaryIO | None:
        return cast(BinaryIO | None, self._process.stdin)

    @property
    def stdout(self) -> BinaryIO | None:
        if self._spec.stdout.disposition != "pipe":
            return None
        return cast(BinaryIO | None, self._process.stdout)

    @property
    def stderr(self) -> BinaryIO | None:
        if self._spec.stderr.disposition != "pipe":
            return None
        return cast(BinaryIO | None, self._process.stderr)

    @property
    def done(self) -> Future[ProcessOutcome]:
        return self._done_task

    def read_stdout(self, from_offset: int = 0) -> ProcessOutputRead | None:
        if self._stdout_collector is None:
            return None
        return self._stdout_collector.read_from(from_offset)

    def read_stderr(self, from_offset: int = 0) -> ProcessOutputRead | None:
        if self._stderr_collector is None:
            return None
        return self._stderr_collector.read_from(from_offset)

    async def terminate(self, *, timed_out: bool = False) -> ProcessOutcome:
        async with self._termination_lock:
            self._cancel_requested = True
            self._timed_out = self._timed_out or timed_out
            if self._done_task.done():
                return await self._done_task
            if self._job is not None and self._job.available:
                await asyncio.to_thread(self._job.terminate)
            elif os.name != "nt":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self._process.pid, signal.SIGTERM)
                if not await self.wait_for_exit(self._spec.grace_seconds):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self._process.pid, signal.SIGKILL)
            else:
                await asyncio.to_thread(
                    _terminate_psutil_tree,
                    self._process.pid,
                    self._create_time,
                    self._spec.grace_seconds,
                )
        if not await self.wait_for_exit(self._spec.grace_seconds):
            raise ProcessTerminationError(
                f"process tree termination was not confirmed for PID {self._process.pid}"
            )
        outcome = await self._done_task
        if not outcome.tree_quiescent:
            raise ProcessTerminationError(
                f"process tree termination was not confirmed for PID {self._process.pid}"
            )
        return outcome

    async def wait_for_exit(self, timeout_seconds: float | None = None) -> bool:
        try:
            if timeout_seconds is None:
                await asyncio.shield(self._done_task)
            else:
                await asyncio.wait_for(asyncio.shield(self._done_task), timeout_seconds)
        except TimeoutError:
            return False
        return True

    async def aclose(self) -> None:
        if not self._done_task.done():
            await self.terminate()
        else:
            outcome = await self._done_task
            if not outcome.tree_quiescent:
                raise ProcessTerminationError(
                    f"process tree termination was not confirmed for PID {self._process.pid}"
                )
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def _collector_task(
        self,
        stream: BinaryIO | None,
        collector: _BoundedCollector | None,
    ) -> Task[None] | None:
        if stream is None or collector is None:
            return None
        return asyncio.create_task(_collect_stream(stream, collector))

    async def _wait_for_outcome(self) -> ProcessOutcome:
        exit_code = await asyncio.to_thread(self._process.wait)
        job_supervised = self._job is not None and self._job.available
        tree_quiescent = await self._wait_for_tree_quiescence()
        tasks = tuple(task for task in (self._stdout_task, self._stderr_task) if task is not None)
        collectors_closed = await self._finish_collectors(tasks)
        tree_quiescent = tree_quiescent and collectors_closed
        if self._stdout_collector is not None:
            self._stdout_collector.close()
        if self._stderr_collector is not None:
            self._stderr_collector.close()
        supervision = "full" if job_supervised else "partial"
        detail = None
        if self._job is not None and not self._job.available:
            detail = self._job.error
        if self._job is not None:
            self._job.close()
        terminating_signal = -exit_code if exit_code < 0 else None
        return ProcessOutcome(
            exit_code=None if exit_code < 0 else exit_code,
            signal=terminating_signal,
            timed_out=self._timed_out,
            cancel_requested=self._cancel_requested,
            cancel_confirmed=self._cancel_requested and tree_quiescent,
            tree_quiescent=tree_quiescent,
            stdout_truncated=(
                self._stdout_collector.truncated if self._stdout_collector is not None else False
            ),
            stderr_truncated=(
                self._stderr_collector.truncated if self._stderr_collector is not None else False
            ),
            supervision=supervision,
            supervision_detail=detail,
        )

    async def _wait_for_tree_quiescence(self) -> bool:
        if self._job is not None and self._job.available:
            active = await asyncio.to_thread(self._job.active_processes)
            if active:
                identities = await asyncio.to_thread(
                    _process_identities,
                    self._job.process_ids(),
                )
                self._job.close()
                return await _wait_for_process_identities_exit(
                    identities,
                    self._spec.grace_seconds,
                )
            return True
        if os.name != "nt":
            return await _terminate_process_group(
                self._process.pid,
                self._spec.grace_seconds,
            )
        return not _matching_process_tree_exists(self._process.pid, self._create_time)

    async def _finish_collectors(self, tasks: tuple[Task[None], ...]) -> bool:
        if not tasks:
            return True
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks),
                timeout=self._spec.grace_seconds,
            )
        except TimeoutError:
            for stream in (self._process.stdout, self._process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            return False
        return True


class LocalProcessRuntime:
    def __init__(self) -> None:
        self._processes: set[LocalManagedProcess] = set()
        self._closed = False

    async def spawn(self, spec: ProcessSpawnSpec) -> LocalManagedProcess:
        if self._closed:
            raise RuntimeError("process runtime is closed")
        if not spec.cwd.is_dir():
            raise NotADirectoryError(spec.cwd)
        environment = scrubbed_parent_environment()
        for name, value in spec.env.items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        stdin = _input_target(spec.stdin)
        stdout = _output_target(spec.stdout)
        stderr = _output_target(spec.stderr)
        creation_flags = 0
        start_new_session = os.name != "nt"
        if os.name == "nt":
            creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        process = await asyncio.to_thread(
            _spawn_process,
            spec,
            environment,
            stdin,
            stdout,
            stderr,
            creation_flags,
            start_new_session,
        )
        raw_handle = int(getattr(process, "_handle", 0))
        job = _WindowsJob(raw_handle) if os.name == "nt" and raw_handle else None
        managed = LocalManagedProcess(
            process,
            spec=spec,
            stdout_collector=(
                _BoundedCollector(spec.stdout) if spec.stdout.disposition == "collect" else None
            ),
            stderr_collector=(
                _BoundedCollector(spec.stderr) if spec.stderr.disposition == "collect" else None
            ),
            job=job,
        )
        self._processes.add(managed)
        managed.done.add_done_callback(
            lambda completed: self._forget_if_quiescent(managed, completed)
        )
        return managed

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await asyncio.gather(
            *(process.aclose() for process in tuple(self._processes)),
            return_exceptions=False,
        )

    def _forget_if_quiescent(
        self,
        managed: LocalManagedProcess,
        completed: Future[ProcessOutcome],
    ) -> None:
        if completed.cancelled():
            return
        try:
            outcome = completed.result()
        except BaseException:
            return
        if outcome.tree_quiescent:
            self._processes.discard(managed)


async def _collect_stream(stream: BinaryIO, collector: _BoundedCollector) -> None:
    read = getattr(stream, "read1", stream.read)
    while True:
        chunk = await asyncio.to_thread(read, _READ_SIZE)
        if not chunk:
            return
        collector.append(chunk)


def _input_target(disposition: str) -> int | None:
    if disposition == "discard":
        return subprocess.DEVNULL
    if disposition == "pipe":
        return subprocess.PIPE
    return None


def _spawn_process(
    spec: ProcessSpawnSpec,
    environment: Mapping[str, str],
    stdin: int | None,
    stdout: int | None,
    stderr: int | None,
    creation_flags: int,
    start_new_session: bool,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        spec.argv,
        cwd=spec.cwd,
        env=environment,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        shell=False,
        creationflags=creation_flags,
        start_new_session=start_new_session,
    )


def _output_target(spec: OutputCaptureSpec) -> int | None:
    if spec.disposition == "discard":
        return subprocess.DEVNULL
    if spec.disposition in {"pipe", "collect"}:
        return subprocess.PIPE
    return None


def _matching_process_tree_exists(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        if create_time and process.create_time() != create_time:
            return False
        return process.is_running() or any(child.is_running() for child in process.children(True))
    except (psutil.Error, OSError):
        return False


def _process_identities(pids: Iterable[int]) -> tuple[tuple[int, float], ...]:
    identities: list[tuple[int, float]] = []
    for pid in pids:
        try:
            identities.append((pid, psutil.Process(pid).create_time()))
        except (psutil.Error, OSError):
            continue
    return tuple(identities)


async def _wait_for_process_identities_exit(
    identities: tuple[tuple[int, float], ...],
    timeout_seconds: float,
) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while any(_same_process_exists(pid, create_time) for pid, create_time in identities):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.025)
    return True


def _same_process_exists(pid: int, create_time: float) -> bool:
    try:
        process = psutil.Process(pid)
        return process.create_time() == create_time and process.is_running()
    except (psutil.Error, OSError):
        return False


async def _terminate_process_group(pid: int, grace_seconds: float) -> bool:
    if not _process_group_exists(pid):
        return True
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    if await _wait_for_process_group_exit(pid, grace_seconds):
        return True
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)
    return await _wait_for_process_group_exit(pid, grace_seconds)


async def _wait_for_process_group_exit(pid: int, timeout_seconds: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while _process_group_exists(pid):
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.025)
    return True


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_psutil_tree(
    pid: int,
    create_time: float,
    grace_seconds: float,
) -> None:
    try:
        process = psutil.Process(pid)
        if create_time and process.create_time() != create_time:
            return
    except (psutil.Error, OSError):
        return
    descendants = process.children(recursive=True)
    targets: list[psutil.Process] = [*reversed(descendants), process]
    _signal_processes(targets, terminate=True)
    _, alive = psutil.wait_procs(targets, timeout=grace_seconds)
    _signal_processes(alive, terminate=False)
    if alive:
        psutil.wait_procs(alive, timeout=grace_seconds)


def _signal_processes(processes: Iterable[psutil.Process], *, terminate: bool) -> None:
    for process in processes:
        try:
            process.terminate() if terminate else process.kill()
        except (psutil.Error, OSError):
            continue
