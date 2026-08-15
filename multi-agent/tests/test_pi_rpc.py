from __future__ import annotations

import asyncio
import json
import unittest

from multi_agent.coordination.pi_rpc import PiRpcClient


class _FakeStdin:
    def __init__(self, process: "_FakeProcess") -> None:
        self.process = process
        self.buffer = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buffer += data
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            if not line:
                continue
            command = json.loads(line.decode("utf-8"))
            self.process.commands.append(command)
            request_id = command["id"]
            command_type = command["type"]
            if command_type == "get_state":
                self.process.feed(
                    {
                        "id": request_id,
                        "type": "response",
                        "command": "get_state",
                        "success": True,
                        "data": {"sessionId": "pi-session", "isStreaming": False},
                    }
                )
            elif command_type == "prompt":
                self.process.feed(
                    {
                        "id": request_id,
                        "type": "response",
                        "command": "prompt",
                        "success": True,
                    }
                )
                self.process.feed({"type": "agent_start"})
                self.process.feed(
                    {
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": '{"ok":true}'}],
                        },
                    }
                )
                self.process.feed({"type": "agent_settled"})
            elif command_type == "abort":
                self.process.feed(
                    {
                        "id": request_id,
                        "type": "response",
                        "command": "abort",
                        "success": True,
                    }
                )

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.process.returncode = 0
        self.process.stdout.feed_eof()
        self.process.exited.set()

    async def wait_closed(self) -> None:
        return None


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.commands: list[dict] = []
        self.returncode: int | None = None
        self.exited = asyncio.Event()
        self.stdin = _FakeStdin(self)

    def feed(self, message: dict) -> None:
        self.stdout.feed_data((json.dumps(message) + "\n").encode("utf-8"))

    async def wait(self) -> int:
        await self.exited.wait()
        return self.returncode or 0

    def terminate(self) -> None:
        self.stdin.close()

    def kill(self) -> None:
        self.stdin.close()


class PiRpcClientFakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_isolated_command_handshake_and_prompt(self) -> None:
        process = _FakeProcess()
        spawn: dict = {}

        async def factory(*args, **kwargs):
            spawn["args"] = list(args)
            spawn["kwargs"] = kwargs
            return process

        client = PiRpcClient(process_factory=factory, timeout_seconds=1)
        async with client:
            result = await client.prompt("evaluate this boundary")

        self.assertEqual(result.session_id, "pi-session")
        self.assertEqual(result.text, '{"ok":true}')
        self.assertEqual(process.commands[0]["type"], "get_state")
        self.assertEqual(process.commands[1]["type"], "prompt")
        self.assertEqual(process.commands[1]["message"], "evaluate this boundary")
        self.assertIn("--mode", spawn["args"])
        self.assertIn("rpc", spawn["args"])
        self.assertIn("--no-tools", spawn["args"])
        self.assertIn("--no-extensions", spawn["args"])
        self.assertIn("--no-themes", spawn["args"])
        self.assertIn("--no-context-files", spawn["args"])
        self.assertIn("--offline", spawn["args"])
