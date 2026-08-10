from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from multi_agent.coordination.base import ContractAdvisor
from multi_agent.coordination.models import (
    AdvisorDraft,
    AdvisorEnvelope,
    ContractCheckRequest,
)
from multi_agent.coordination.pi_rpc import PiRpcClient
from multi_agent.domain.errors import CoordinatorOutputError

PiClientFactory = Callable[[], PiRpcClient]


class PiContractAdvisor(ContractAdvisor):
    name = "pi"

    def __init__(
        self,
        *,
        executable: str = "pi",
        cwd: Path | str | None = None,
        provider: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 180.0,
        client_factory: PiClientFactory | None = None,
    ) -> None:
        self.executable = executable
        self.cwd = None if cwd is None else str(Path(cwd).resolve())
        self.provider = provider
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transport": "jsonl-rpc",
            "executable": self.executable,
            "resolved_executable": shutil.which(self.executable),
            "isolated_process_per_evaluation": True,
            "session_persistence": False,
            "builtin_tools": [],
            "extensions": False,
            "skills": False,
            "context_files": False,
            "provider_configured": self.provider is not None,
            "model_configured": self.model is not None,
        }

    async def evaluate(self, request: ContractCheckRequest) -> AdvisorEnvelope:
        client = self._make_client()
        async with client:
            result = await client.prompt(self._build_prompt(request))
        return AdvisorEnvelope(
            draft=self._parse_draft(result.text),
            advisor_session_id=result.session_id,
            event_count=len(result.events),
        )

    def _make_client(self) -> PiRpcClient:
        if self._client_factory is not None:
            return self._client_factory()
        return PiRpcClient(
            executable=self.executable,
            cwd=self.cwd,
            provider=self.provider,
            model=self.model,
            timeout_seconds=self.timeout_seconds,
        )

    @staticmethod
    def _build_prompt(request: ContractCheckRequest) -> str:
        payload = request.model_dump(mode="json")
        schema = AdvisorDraft.model_json_schema()
        return (
            "You are a narrow contract advisor at the boundary of a deterministic "
            "multi-agent orchestrator. Return exactly one JSON object and no markdown "
            "or commentary. You may only: judge admit/reject, provide a small normalized "
            "replacement value when action=revise, and recommend IDs from the supplied "
            "candidate_next_steps. Never create or modify workflows or tasks. Never "
            "choose providers, models, prompts, workspaces, permissions, tools, retries, "
            "sessions, concurrency, or execution parameters. Never trigger execution. "
            "The deterministic service validates your response and owns the final gate.\n\n"
            f"BOUNDARY_REQUEST:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
            f"RESPONSE_JSON_SCHEMA:\n{json.dumps(schema, ensure_ascii=False)}"
        )

    @staticmethod
    def _parse_draft(text: str) -> AdvisorDraft:
        candidate = text.strip()
        if candidate.startswith("```"):
            first_newline = candidate.find("\n")
            last_fence = candidate.rfind("```")
            if first_newline < 0 or last_fence <= first_newline:
                raise CoordinatorOutputError("Pi returned an incomplete JSON code fence")
            candidate = candidate[first_newline + 1 : last_fence].strip()
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise CoordinatorOutputError(
                f"Pi returned invalid advisor JSON at line {exc.lineno}, column {exc.colno}"
            ) from exc
        try:
            return AdvisorDraft.model_validate(value)
        except ValidationError as exc:
            raise CoordinatorOutputError(
                f"Pi advisor output failed schema validation: {exc}"
            ) from exc
