from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from a2a.types.a2a_pb2 import TaskState
from google.protobuf.json_format import MessageToDict
from misaka_a2a_capability import TaskRequest
from misaka_a2a_http import A2AHttpClient
from misaka_agent_capability import AGENT_CAPABILITY_ID, AGENT_OPERATION_INVOKE
from misaka_invocation_contracts import CapabilityFeature


async def run(base_url: str) -> None:
    suffix = uuid.uuid4().hex[:12]
    request = TaskRequest(
        task_id=f"task-{suffix}",
        context_id=f"context-{suffix}",
        message_id=f"message-{suffix}",
        idempotency_key=f"idem-{suffix}",
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
    async with A2AHttpClient(base_url) as client:
        task = await client.send(request)
    payload = MessageToDict(task)
    payload["stateName"] = TaskState.Name(task.status.state)
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def main() -> None:
    parser = argparse.ArgumentParser(description="Call a standalone Multi-Agent V3 A2A node")
    parser.add_argument("--base-url", default="http://127.0.0.1:8015")
    args = parser.parse_args()
    asyncio.run(run(args.base_url))


if __name__ == "__main__":
    main()
