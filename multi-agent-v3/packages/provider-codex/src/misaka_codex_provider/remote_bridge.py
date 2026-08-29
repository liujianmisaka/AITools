from __future__ import annotations

import argparse
import asyncio
import sys
import threading
from typing import Protocol, cast

from websockets.asyncio.client import ClientConnection, connect


class _ReconfigurableTextIO(Protocol):
    def reconfigure(
        self,
        *,
        encoding: str | None = None,
        newline: str | None = None,
    ) -> None: ...


async def bridge(url: str) -> None:
    async with connect(url, compression=None, max_size=None) as socket:
        input_lines: asyncio.Queue[str | None] = asyncio.Queue()
        _start_stdin_reader(asyncio.get_running_loop(), input_lines)
        sender = asyncio.create_task(_forward_stdin(socket, input_lines))
        receiver = asyncio.create_task(_forward_websocket(socket))
        done, pending = await asyncio.wait(
            {sender, receiver},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            task.result()


def _start_stdin_reader(
    loop: asyncio.AbstractEventLoop,
    input_lines: asyncio.Queue[str | None],
) -> None:
    def read() -> None:
        while line := sys.stdin.readline():
            try:
                loop.call_soon_threadsafe(input_lines.put_nowait, line)
            except RuntimeError:
                return
        try:
            loop.call_soon_threadsafe(input_lines.put_nowait, None)
        except RuntimeError:
            return

    threading.Thread(target=read, name="codex-remote-bridge-stdin", daemon=True).start()


async def _forward_stdin(
    socket: ClientConnection,
    input_lines: asyncio.Queue[str | None],
) -> None:
    while (line := await input_lines.get()) is not None:
        await socket.send(line.rstrip("\r\n"))
    await socket.close()


async def _forward_websocket(socket: ClientConnection) -> None:
    async for message in socket:
        text = message.decode("utf-8") if isinstance(message, bytes) else message
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


def main() -> None:
    cast(_ReconfigurableTextIO, sys.stdin).reconfigure(encoding="utf-8")
    cast(_ReconfigurableTextIO, sys.stdout).reconfigure(encoding="utf-8", newline="\n")
    parser = argparse.ArgumentParser(description="Bridge Codex SDK stdio to App Server WebSocket")
    parser.add_argument("--url", required=True)
    args = parser.parse_args()
    asyncio.run(bridge(args.url))


if __name__ == "__main__":
    main()
