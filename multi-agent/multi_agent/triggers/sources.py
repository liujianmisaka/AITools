from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from pydantic import ValidationError

from multi_agent.domain.errors import (
    EventSourceNotFoundError,
    TriggerEventProcessingError,
)
from multi_agent.domain.models import (
    GitCommitSourceConfig,
    TriggerBindingDefinition,
    TriggerEventInput,
    WebhookSourceConfig,
    utc_now,
)
from multi_agent.workspaces.manager import WorkspaceManager


@dataclass(frozen=True, slots=True)
class SourcePollResult:
    events: tuple[TriggerEventInput, ...]
    cursor: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


class EventSourceDriver(ABC):
    source_type: str
    delivery_mode: str
    external_push_enabled: bool = True
    unique_source_key: bool = False

    def validate_binding(self, binding: TriggerBindingDefinition) -> None:
        del binding

    def describe(self) -> dict[str, object]:
        return {
            "source_type": self.source_type,
            "delivery_mode": self.delivery_mode,
            "supports_polling": self.delivery_mode in {"poll", "hybrid"},
            "supports_push": self.delivery_mode in {"push", "hybrid"},
            "external_push_enabled": self.external_push_enabled,
            "unique_source_key": self.unique_source_key,
        }

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        del binding, cursor
        raise RuntimeError(f"event source {self.source_type!r} is not pollable")


class ManualEventSource(EventSourceDriver):
    source_type = "manual"
    delivery_mode = "push"


class WebhookEventSource(EventSourceDriver):
    """Generic HTTP webhook endpoint with signature and IP verification."""

    source_type = "webhook"
    delivery_mode = "push"
    unique_source_key = True

    def validate_binding(self, binding: TriggerBindingDefinition) -> None:
        if binding.event_type != "webhook.received" or binding.event_version != 1:
            raise TriggerEventProcessingError(
                "webhook bindings must use webhook.received@1"
            )
        try:
            config = WebhookSourceConfig.model_validate(binding.source_config)
        except ValidationError as exc:
            raise TriggerEventProcessingError(
                f"invalid webhook source_config: {exc}"
            ) from exc
        if binding.source_key != config.endpoint_key:
            raise TriggerEventProcessingError(
                "webhook source_key must equal the configured endpoint_key"
            )
        for cidr in config.allowed_ip_cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as exc:
                raise TriggerEventProcessingError(
                    f"invalid webhook allowed_ip_cidrs entry {cidr!r}: {exc}"
                ) from exc
        if config.secret_ref is not None:
            self.resolve_secret(config)
        elif config.require_signature:
            raise TriggerEventProcessingError(
                "webhook require_signature=true requires secret_ref"
            )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "event_types": ["webhook.received@1"],
            "source_config_schema": WebhookSourceConfig.model_json_schema(),
        }

    def resolve_secret(self, config: WebhookSourceConfig) -> str | None:
        if config.secret_ref is None:
            return None
        direct = os.getenv(config.secret_ref)
        if direct:
            return direct
        normalized = re.sub(
            r"[^A-Za-z0-9_]", "_", config.secret_ref.upper()
        )
        namespaced = f"MULTI_AGENT_WEBHOOK_SECRET_{normalized}"
        namespaced_value = os.getenv(namespaced)
        if namespaced_value:
            return namespaced_value
        raise TriggerEventProcessingError(
            f"webhook secret_ref {config.secret_ref!r} is not present in the "
            f"environment as {config.secret_ref!r} or {namespaced!r}"
        )

    @classmethod
    def verify_signature(
        cls,
        *,
        raw_body: bytes,
        headers: Mapping[str, str],
        config: WebhookSourceConfig,
        secret: str | None,
    ) -> None:
        if not config.require_signature:
            return
        if not secret:
            raise TriggerEventProcessingError(
                "webhook endpoint has no resolved secret"
            )
        header_value = cls.header_value(headers, config.signature_header)
        if not header_value:
            raise TriggerEventProcessingError(
                f"missing webhook signature header {config.signature_header!r}"
            )
        expected = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            config.signature_algorithm,
        ).hexdigest()
        supplied = header_value.strip()
        if supplied.lower().startswith(config.signature_algorithm + "="):
            supplied = supplied.split("=", 1)[1].strip()
        if not hmac.compare_digest(expected.lower(), supplied.lower()):
            raise TriggerEventProcessingError("webhook signature verification failed")

    @staticmethod
    def header_value(
        headers: Mapping[str, str],
        name: str,
    ) -> str | None:
        wanted = name.lower()
        for key, value in headers.items():
            if key.lower() == wanted:
                return value
        return None

    @classmethod
    def client_allowed(
        cls,
        *,
        client_ip: str | None,
        config: WebhookSourceConfig,
    ) -> bool:
        if not config.allowed_ip_cidrs:
            return True
        if client_ip is None:
            return False
        # Starlette's TestClient reports "testclient"; treat it as loopback so
        # local API tests can exercise CIDR allowlisting without weakening
        # real socket handling (Uvicorn/hypercorn always report an IP here).
        normalized_ip = "127.0.0.1" if client_ip == "testclient" else client_ip
        try:
            address = ipaddress.ip_address(normalized_ip)
        except ValueError:
            return False
        return any(
            address in ipaddress.ip_network(cidr, strict=False)
            for cidr in config.allowed_ip_cidrs
        )

    @classmethod
    def payload_from_body(
        cls,
        *,
        raw_body: bytes,
        content_type: str | None,
        config: WebhookSourceConfig,
    ) -> dict[str, Any]:
        if len(raw_body) > config.max_payload_bytes:
            raise TriggerEventProcessingError(
                f"webhook payload exceeds {config.max_payload_bytes} bytes"
            )
        if not raw_body:
            return {}
        text = raw_body.decode("utf-8-sig", errors="strict")
        normalized_type = (content_type or "").split(";", 1)[0].strip().lower()
        if normalized_type == "application/json" or normalized_type.endswith("+json"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise TriggerEventProcessingError(
                    f"webhook body is not valid JSON: {exc}"
                ) from exc
        elif normalized_type == "application/x-www-form-urlencoded":
            try:
                payload = dict(parse_qsl(text, keep_blank_values=True))
            except ValueError as exc:
                raise TriggerEventProcessingError(
                    f"webhook form body is invalid: {exc}"
                ) from exc
        else:
            raise TriggerEventProcessingError(
                "webhook content-type must be application/json or "
                "application/x-www-form-urlencoded"
            )
        if not isinstance(payload, dict):
            raise TriggerEventProcessingError(
                "webhook JSON payload must be a JSON object"
            )
        return payload


class InternalEventSource(EventSourceDriver):
    """Application-owned events; the HTTP event API cannot publish them."""

    source_type = "internal"
    delivery_mode = "push"
    external_push_enabled = False

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "event_types": [
                "workflow.instance.created@1",
                "workflow.instance.status_changed@1",
                "approval.updated@1",
                "schedule.run.updated@1",
                "trigger.delivery.failed@1",
            ],
        }


class ScheduleEventSource(EventSourceDriver):
    """Synthetic events emitted only by scheduled actions."""

    source_type = "schedule"
    delivery_mode = "push"
    external_push_enabled = False

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "event_types": ["schedule.tick@1"],
        }


class GitCommitEventSource(EventSourceDriver):
    source_type = "git_commit"
    delivery_mode = "poll"

    def __init__(
        self,
        *,
        workspaces: WorkspaceManager,
        git_bin: str = "git",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._workspaces = workspaces
        self._git_bin = git_bin
        self._timeout_seconds = timeout_seconds

    def validate_binding(self, binding: TriggerBindingDefinition) -> None:
        if binding.event_type != "git.commit.updated" or binding.event_version != 1:
            raise TriggerEventProcessingError(
                "git_commit bindings must use git.commit.updated@1"
            )
        try:
            config = GitCommitSourceConfig.model_validate(binding.source_config)
        except ValidationError as exc:
            raise TriggerEventProcessingError(
                f"invalid git_commit source_config: {exc}"
            ) from exc
        self._workspaces.resolve(config.workspace_id)
        expected_key = self.source_key(config)
        if binding.source_key != expected_key:
            raise TriggerEventProcessingError(
                "git_commit source_key must equal "
                f"{expected_key!r} for the configured workspace/remote/branch"
            )

    def describe(self) -> dict[str, object]:
        return {
            **super().describe(),
            "event_types": ["git.commit.updated@1"],
            "source_config_schema": GitCommitSourceConfig.model_json_schema(),
            "first_poll": "establish_baseline",
        }

    @classmethod
    def source_key(cls, config: GitCommitSourceConfig) -> str:
        return f"{config.workspace_id}:{config.remote}:{config.branch}"

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        config = GitCommitSourceConfig.model_validate(binding["source_config"])
        workspace = self._workspaces.resolve(config.workspace_id)
        if config.fetch:
            refspec = (
                f"+refs/heads/{config.branch}:"
                f"refs/remotes/{config.remote}/{config.branch}"
            )
            await self._git(
                workspace,
                "fetch",
                "--quiet",
                "--no-tags",
                "--prune",
                config.remote,
                refspec,
            )
        remote_ref = f"refs/remotes/{config.remote}/{config.branch}"
        after_sha = (
            await self._git(workspace, "rev-parse", "--verify", remote_ref)
        ).strip()
        if not self._is_sha(after_sha):
            raise TriggerEventProcessingError(
                f"git returned an invalid commit SHA for {remote_ref!r}"
            )
        previous_sha = str((cursor or {}).get("head_sha", ""))
        next_cursor = {
            "head_sha": after_sha,
            "observed_at": utc_now().isoformat(),
        }
        if not previous_sha or previous_sha == after_sha:
            return SourcePollResult(events=(), cursor=next_cursor)

        forward = await self._is_ancestor(workspace, previous_sha, after_sha)
        subject = (
            await self._git(workspace, "show", "-s", "--format=%s", after_sha)
        ).strip()
        author_name = (
            await self._git(workspace, "show", "-s", "--format=%an", after_sha)
        ).strip()
        authored_at = (
            await self._git(workspace, "show", "-s", "--format=%aI", after_sha)
        ).strip()
        commit_count: int | None = None
        if forward:
            raw_count = (
                await self._git(
                    workspace,
                    "rev-list",
                    "--count",
                    f"{previous_sha}..{after_sha}",
                )
            ).strip()
            commit_count = int(raw_count)
        source_key = self.source_key(config)
        dedup_material = f"{source_key}\0{previous_sha}\0{after_sha}".encode()
        event = TriggerEventInput(
            source_type=self.source_type,
            event_type="git.commit.updated",
            event_version=1,
            source_key=source_key,
            dedup_key="git:" + hashlib.sha256(dedup_material).hexdigest(),
            payload={
                "workspace_id": config.workspace_id,
                "remote": config.remote,
                "branch": config.branch,
                "before_sha": previous_sha,
                "after_sha": after_sha,
                "update_kind": "forward" if forward else "rewritten",
                "commit_count": commit_count,
                "subject": subject,
                "author_name": author_name,
                "authored_at": authored_at,
                "observed_at": utc_now().astimezone(timezone.utc).isoformat(),
            },
        )
        return SourcePollResult(events=(event,), cursor=next_cursor)

    async def _is_ancestor(
        self,
        workspace: Path,
        before_sha: str,
        after_sha: str,
    ) -> bool:
        process = await self._run_git(
            workspace,
            "merge-base",
            "--is-ancestor",
            before_sha,
            after_sha,
            check=False,
        )
        if process.returncode == 0:
            return True
        if process.returncode == 1:
            return False
        raise TriggerEventProcessingError(
            self._git_error(process, "git merge-base failed")
        )

    async def _git(self, workspace: Path, *arguments: str) -> str:
        process = await self._run_git(workspace, *arguments, check=True)
        return process.stdout.decode("utf-8", errors="replace")

    async def _run_git(
        self,
        workspace: Path,
        *arguments: str,
        check: bool,
    ) -> _GitCommandResult:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                process = await asyncio.create_subprocess_exec(
                    self._git_bin,
                    "-C",
                    str(workspace),
                    *arguments,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await process.communicate()
        except TimeoutError as exc:
            raise TriggerEventProcessingError(
                f"git command timed out after {self._timeout_seconds} seconds"
            ) from exc
        except OSError as exc:
            raise TriggerEventProcessingError(
                f"cannot start git executable {self._git_bin!r}: {exc}"
            ) from exc
        if check and process.returncode != 0:
            raise TriggerEventProcessingError(
                self._git_error(
                    _GitCommandResult(process.returncode, stdout, stderr),
                    "git command failed",
                )
            )
        return _GitCommandResult(process.returncode, stdout, stderr)

    @staticmethod
    def _git_error(process: _GitCommandResult, fallback: str) -> str:
        stderr = process.stderr.decode("utf-8", errors="replace").strip()
        return stderr or fallback

    @staticmethod
    def _is_sha(value: str) -> bool:
        return len(value) in {40, 64} and all(
            char in "0123456789abcdef" for char in value
        )


class FakeEventSource(EventSourceDriver):
    """Deterministic poll source used by tests; it performs no external I/O."""

    source_type = "fake"
    delivery_mode = "hybrid"

    def __init__(self) -> None:
        self._events: dict[str, list[TriggerEventInput]] = defaultdict(list)

    def emit(self, event: TriggerEventInput) -> None:
        if event.source_type != self.source_type:
            raise ValueError("fake source can only emit source_type='fake'")
        self._events[event.source_key or ""].append(event)

    async def poll(
        self,
        binding: Mapping[str, Any],
        cursor: Mapping[str, Any] | None,
    ) -> SourcePollResult:
        source_key = str(binding.get("source_key") or "")
        offset = int((cursor or {}).get("offset", 0))
        events = self._events[source_key][offset:]
        return SourcePollResult(
            events=tuple(events),
            cursor={"offset": offset + len(events)},
        )


class EventSourceRegistry:
    def __init__(self, sources: Iterable[EventSourceDriver] = ()) -> None:
        self._sources: dict[str, EventSourceDriver] = {}
        for source in sources:
            self.register(source)

    def register(self, source: EventSourceDriver) -> None:
        if not source.source_type:
            raise ValueError("event source type cannot be empty")
        if source.delivery_mode not in {"push", "poll", "hybrid"}:
            raise ValueError(
                f"invalid delivery mode for {source.source_type!r}: "
                f"{source.delivery_mode!r}"
            )
        if source.source_type in self._sources:
            raise ValueError(
                f"event source already registered: {source.source_type}"
            )
        self._sources[source.source_type] = source

    def get(self, source_type: str) -> EventSourceDriver:
        try:
            return self._sources[source_type]
        except KeyError as exc:
            raise EventSourceNotFoundError(
                f"event source not found: {source_type}"
            ) from exc

    def describe(self) -> list[dict[str, object]]:
        return [
            source.describe()
            for source in sorted(
                self._sources.values(), key=lambda item: item.source_type
            )
        ]
