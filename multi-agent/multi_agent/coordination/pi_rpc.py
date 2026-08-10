from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from multi_agent.domain.errors import CoordinatorOutputError, CoordinatorUnavailableError

ProcessFactory = Callable[..., Awaitable[Any]]


@dataclass(slots=True, frozen=True)
class PiPromptResult:
    session_id: str | None
    text: str
    events: tuple[dict[str, Any], ...]


class PiRpcClient:
    """One isolated Pi RPC subprocess using strict LF-delimited JSONL."""

    def __init__(
        self,
        *,
        executable: str = "pi",
        cwd: Path | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 180.0,
        process_factory: ProcessFactory | None = None,
    ) -> None:
        self.executable = executable
        self.cwd = None if cwd is None else str(Path(cwd).resolve())
        self.provider = provider
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._process: Any | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._prompt_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._stderr_tail: list[str] = []
        self._session_id: str | None = None

    @property
    def command(self) -> list[str]:
        command = [
            self.executable,
            "--mode",
            "rpc",
            "--no-session",
            "--no-tools",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-context-files",
            "--no-approve",
            "--offline",
        ]
        if self.provider:
            command.extend(["--provider", self.provider])
        if self.model:
            command.extend(["--model", self.model])
        return command

    async def __aenter__(self) -> "PiRpcClient":
        await self.start()
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.close()

    async def start(self) -> dict[str, Any]:
        if self._process is not None:
            return await self.get_state()
        try:
            async with asyncio.timeout(min(self.timeout_seconds, 30.0)):
                self._process = await self._process_factory(
                    *self.command,
                    cwd=self.cwd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
        except (FileNotFoundError, OSError) as exc:
            raise CoordinatorUnavailableError(
                f"cannot start Pi RPC executable {self.executable!r}: {exc}"
            ) from exc
        except TimeoutError as exc:
            raise CoordinatorUnavailableError("timed out while starting Pi RPC") from exc
        if self._process.stdin is None or self._process.stdout is None:
            await self.close()
            raise CoordinatorUnavailableError("Pi RPC process did not expose stdin/stdout")
        self._reader_task = asyncio.create_task(
            self._read_stdout(), name="pi-rpc-stdout"
        )
        if self._process.stderr is not None:
            self._stderr_task = asyncio.create_task(
                self._read_stderr(), name="pi-rpc-stderr"
            )
        try:
            async with asyncio.timeout(min(self.timeout_seconds, 30.0)):
                state = await self.get_state()
        except BaseException:
            await self.close()
            raise
        session_id = state.get("sessionId")
        self._session_id = str(session_id) if session_id else None
        return state

    async def close(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return

        stdin = process.stdin
        if stdin is not None:
            stdin.close()
            wait_closed = getattr(stdin, "wait_closed", None)
            if wait_closed is not None:
                try:
                    await wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                process.kill()
                await process.wait()

        tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._fail_pending("Pi RPC process closed")

    async def get_state(self) -> dict[str, Any]:
        if self._process is None:
            return await self.start()
        response = await self._request({"type": "get_state"})
        data = response.get("data")
        if not isinstance(data, dict):
            raise CoordinatorUnavailableError("Pi RPC get_state returned invalid data")
        return data

    async def prompt(self, message: str) -> PiPromptResult:
        async with self._prompt_lock:
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    return await self._prompt(message)
            except TimeoutError as exc:
                try:
                    await self._request({"type": "abort"})
                except CoordinatorUnavailableError:
                    pass
                raise CoordinatorUnavailableError(
                    f"Pi advisor did not settle within {self.timeout_seconds:g} seconds"
                ) from exc

    async def _prompt(self, message: str) -> PiPromptResult:
        if self._process is None:
            await self.start()
        await self._request({"type": "prompt", "message": message})
        events: list[dict[str, Any]] = []
        assistant_text: str | None = None
        fallback_text: str | None = None

        while True:
            event = await self._events.get()
            if event.get("type") == "rpc_eof":
                raise CoordinatorUnavailableError(self._exit_message())
            events.append(event)
            event_type = event.get("type")
            if event_type == "message_end":
                text = self._extract_assistant_text(event.get("message"))
                if text is not None:
                    assistant_text = text
            elif event_type == "agent_end":
                messages = event.get("messages")
                if isinstance(messages, list):
                    for item in reversed(messages):
                        text = self._extract_assistant_text(item)
                        if text is not None:
                            fallback_text = text
                            break
            elif event_type == "agent_settled":
                break

        final_text = assistant_text if assistant_text is not None else fallback_text
        if final_text is None or not final_text.strip():
            raise CoordinatorOutputError("Pi advisor completed without assistant text")
        return PiPromptResult(
            session_id=self._session_id,
            text=final_text,
            events=tuple(events),
        )

    async def _request(self, command: dict[str, Any]) -> dict[str, Any]:
        if self._process is None and command.get("type") != "get_state":
            await self.start()
        process = self._process
        if process is None or process.stdin is None:
            raise CoordinatorUnavailableError("Pi RPC process is not running")
        if process.returncode is not None:
            raise CoordinatorUnavailableError(self._exit_message())

        request_id = uuid4().hex
        payload = dict(command)
        payload["id"] = request_id
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
            response = await future
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending.pop(request_id, None)
            raise CoordinatorUnavailableError(self._exit_message()) from exc
        finally:
            self._pending.pop(request_id, None)

        if response.get("success") is not True:
            error = response.get("error") or response.get("message") or response
            raise CoordinatorUnavailableError(f"Pi RPC command failed: {error}")
        return response

    async def _read_stdout(self) -> None:
        assert self._process is not None
        stdout = self._process.stdout
        assert stdout is not None
        try:
            while True:
                raw = await stdout.readline()
                if not raw:
                    break
                line = raw[:-1] if raw.endswith(b"\n") else raw
                if line.endswith(b"\r"):
                    line = line[:-1]
                if not line:
                    continue
                try:
                    message = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._fail_pending(f"invalid Pi RPC JSONL record: {exc}")
                    await self._events.put({"type": "rpc_eof"})
                    return
                request_id = message.get("id")
                if message.get("type") == "response" and request_id in self._pending:
                    future = self._pending[request_id]
                    if not future.done():
                        future.set_result(message)
                else:
                    await self._events.put(message)
        finally:
            self._fail_pending(self._exit_message())
            await self._events.put({"type": "rpc_eof"})

    async def _read_stderr(self) -> None:
        assert self._process is not None
        stderr = self._process.stderr
        assert stderr is not None
        while True:
            raw = await stderr.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if text:
                self._stderr_tail.append(text)
                del self._stderr_tail[:-20]

    def _fail_pending(self, message: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(CoordinatorUnavailableError(message))

    def _exit_message(self) -> str:
        returncode = None if self._process is None else self._process.returncode
        detail = self._stderr_tail[-1] if self._stderr_tail else "no stderr output"
        return f"Pi RPC process exited (code={returncode}): {detail}"

    @staticmethod
    def _extract_assistant_text(message: Any) -> str | None:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return None
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return None
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts) if parts else None
