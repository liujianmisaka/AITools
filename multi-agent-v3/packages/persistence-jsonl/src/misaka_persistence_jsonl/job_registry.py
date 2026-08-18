from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import cast

from misaka_kernel_contracts import JsonObject
from misaka_persistence_contracts import (
    DurableConflict,
    DurableJob,
    DurableJobStatus,
    DurableNotFound,
)

from misaka_persistence_jsonl.event_log import JsonlEventLog


class JsonlJobRegistry:
    """Rebuildable durable job facts backed by one append-only event stream."""

    _STREAM = "durable.jobs"

    def __init__(self, log: JsonlEventLog) -> None:
        self._log = log
        self._jobs: dict[str, DurableJob] = {}
        self._idempotency: dict[str, str] = {}
        self._loaded = False

    async def open(self) -> None:
        if self._loaded:
            return
        events = await self._log.read(self._STREAM)
        for event in events:
            if event.event_type == "job.created":
                job = _decode_job(event.payload)
                self._jobs[job.job_id] = job
                self._idempotency[job.idempotency_key] = job.job_id
            elif event.event_type == "job.updated":
                job = _decode_job(event.payload)
                self._jobs[job.job_id] = job
            else:
                raise DurableConflict(
                    "durable.unknown_job_event",
                    f"unknown job event type {event.event_type}",
                )
        self._loaded = True

    async def register(
        self,
        job_id: str,
        idempotency_key: str,
        request: JsonObject,
    ) -> tuple[DurableJob, bool]:
        await self.open()
        fingerprint = _fingerprint(request)
        existing_id = self._idempotency.get(idempotency_key)
        if existing_id is not None:
            existing = self._jobs[existing_id]
            if _fingerprint(existing.request) != fingerprint or existing.job_id != job_id:
                raise DurableConflict(
                    "durable.idempotency_conflict",
                    f"idempotency key {idempotency_key} has a different job",
                )
            return existing, False
        existing = self._jobs.get(job_id)
        if existing is not None:
            if (
                existing.idempotency_key != idempotency_key
                or _fingerprint(existing.request) != fingerprint
            ):
                raise DurableConflict(
                    "durable.job_conflict", f"job {job_id} has a different request"
                )
            return existing, False
        now = datetime.now(UTC)
        job = DurableJob(
            job_id=job_id,
            idempotency_key=idempotency_key,
            request=request,
            status=DurableJobStatus.QUEUED,
            created_at=now,
            updated_at=now,
        )
        await self._log.append(
            self._STREAM,
            f"job-created:{job_id}",
            "job.created",
            _encode_job(job),
        )
        self._jobs[job_id] = job
        self._idempotency[idempotency_key] = job_id
        return job, True

    async def get(self, job_id: str) -> DurableJob:
        await self.open()
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise DurableNotFound("durable.job_not_found", f"job {job_id} was not found") from exc

    async def list(self) -> tuple[DurableJob, ...]:
        await self.open()
        return tuple(self._jobs.values())

    async def transition(
        self,
        job_id: str,
        status: DurableJobStatus,
        *,
        expected_version: int | None = None,
        result: JsonObject | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DurableJob:
        current = await self.get(job_id)
        if expected_version is not None and current.version != expected_version:
            raise DurableConflict(
                "durable.version_conflict",
                f"job {job_id} expected version {expected_version}, got {current.version}",
            )
        if current.status in {
            DurableJobStatus.SUCCEEDED,
            DurableJobStatus.FAILED,
            DurableJobStatus.CANCELLED,
            DurableJobStatus.RECONCILIATION_REQUIRED,
        }:
            if current.status is status and current.result == result:
                return current
            raise DurableConflict("durable.terminal_conflict", f"job {job_id} is already terminal")
        updated = DurableJob(
            job_id=current.job_id,
            idempotency_key=current.idempotency_key,
            request=current.request,
            status=status,
            version=current.version + 1,
            result=result,
            error_code=error_code,
            error_message=error_message,
            created_at=current.created_at,
            updated_at=datetime.now(UTC),
        )
        await self._log.append(
            self._STREAM,
            f"job-updated:{job_id}:{updated.version}",
            "job.updated",
            _encode_job(updated),
        )
        self._jobs[job_id] = updated
        return updated


def _fingerprint(value: JsonObject) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _encode_job(job: DurableJob) -> JsonObject:
    return {
        "job_id": job.job_id,
        "idempotency_key": job.idempotency_key,
        "request": job.request,
        "status": job.status.value,
        "version": job.version,
        "result": job.result,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


def _decode_job(payload: JsonObject) -> DurableJob:
    request = payload["request"]
    result = payload.get("result")
    version = payload["version"]
    if not isinstance(request, dict):
        raise ValueError("job request must be an object")
    if result is not None and not isinstance(result, dict):
        raise ValueError("job result must be an object when provided")
    if isinstance(version, bool) or not isinstance(version, (int, str)):
        raise ValueError("job version must be an integer")
    return DurableJob(
        job_id=str(payload["job_id"]),
        idempotency_key=str(payload["idempotency_key"]),
        request=cast(JsonObject, request),
        status=DurableJobStatus(str(payload["status"])),
        version=int(version),
        result=cast(JsonObject, result) if result is not None else None,
        error_code=_optional_str(payload.get("error_code")),
        error_message=_optional_str(payload.get("error_message")),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        updated_at=datetime.fromisoformat(str(payload["updated_at"])),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
