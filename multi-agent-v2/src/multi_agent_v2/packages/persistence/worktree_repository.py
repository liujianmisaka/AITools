from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from multi_agent_v2.packages.persistence.agent_models import WorkspaceWorktree

type RecoverableWorktreeState = Literal[
    "preparing", "ready", "in_use", "cleanup_pending", "cleanup_failed"
]


class CleanupClaimDisposition(StrEnum):
    ACQUIRED = "acquired"
    BUSY = "busy"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class WorktreeRegistration:
    worktree_id: str
    execution_id: str
    workspace_id: str
    relative_path: str
    base_commit: str


@dataclass(frozen=True, slots=True)
class WorktreeCleanupClaim:
    disposition: CleanupClaimDisposition
    execution_id: str
    state: str
    cleanup_epoch: int


class WorktreeStateError(RuntimeError):
    pass


class WorktreeRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def register_preparing(self, registration: WorktreeRegistration) -> None:
        async with self._sessions() as session, session.begin():
            await session.execute(
                pg_insert(WorkspaceWorktree)
                .values(
                    worktree_id=registration.worktree_id,
                    execution_id=registration.execution_id,
                    workspace_id=registration.workspace_id,
                    relative_path=registration.relative_path,
                    base_commit=registration.base_commit,
                    state="preparing",
                    cleanup_attempts=0,
                    cleanup_epoch=0,
                )
                .on_conflict_do_nothing()
            )
            row = await self._load_by_execution(session, registration.execution_id)
            if (
                row.worktree_id != registration.worktree_id
                or row.workspace_id != registration.workspace_id
                or row.relative_path != registration.relative_path
                or row.base_commit != registration.base_commit
            ):
                raise WorktreeStateError(
                    "execution worktree identity was reused with different immutable data"
                )

    async def mark_ready(self, execution_id: str) -> None:
        await self._transition(execution_id, {"preparing", "ready"}, "ready", ready=True)

    async def mark_in_use(self, execution_id: str) -> None:
        await self._transition(execution_id, {"ready", "in_use"}, "in_use")

    async def claim_cleanup(
        self,
        execution_id: str,
        *,
        cleanup_owner: str,
        lease_duration: timedelta,
    ) -> WorktreeCleanupClaim:
        if not cleanup_owner.strip():
            raise ValueError("cleanup owner must not be blank")
        if lease_duration <= timedelta(0):
            raise ValueError("cleanup lease duration must be positive")
        async with self._sessions() as session, session.begin():
            row = await self._load_by_execution(session, execution_id)
            now = await self._database_now(session)
            if row.state in {"cleaned", "preserved"}:
                return WorktreeCleanupClaim(
                    CleanupClaimDisposition.TERMINAL,
                    execution_id,
                    row.state,
                    row.cleanup_epoch,
                )
            if (
                row.state == "cleanup_pending"
                and row.cleanup_lease_expires_at is not None
                and row.cleanup_lease_expires_at > now
            ):
                if row.cleanup_owner == cleanup_owner:
                    row.cleanup_lease_expires_at = now + lease_duration
                    row.updated_at = now
                    return WorktreeCleanupClaim(
                        CleanupClaimDisposition.ACQUIRED,
                        execution_id,
                        row.state,
                        row.cleanup_epoch,
                    )
                return WorktreeCleanupClaim(
                    CleanupClaimDisposition.BUSY,
                    execution_id,
                    row.state,
                    row.cleanup_epoch,
                )
            row.state = "cleanup_pending"
            row.cleanup_attempts += 1
            row.cleanup_epoch += 1
            row.cleanup_owner = cleanup_owner
            row.cleanup_lease_expires_at = now + lease_duration
            row.updated_at = now
            return WorktreeCleanupClaim(
                CleanupClaimDisposition.ACQUIRED,
                execution_id,
                row.state,
                row.cleanup_epoch,
            )

    async def finish_cleanup(
        self,
        execution_id: str,
        *,
        cleanup_owner: str,
        cleanup_epoch: int,
        disposition: Literal["removed", "preserved", "failed"],
        error: str | None = None,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await self._load_by_execution(session, execution_id)
            if row.state in {"cleaned", "preserved"}:
                expected = "cleaned" if disposition == "removed" else "preserved"
                if row.state == expected:
                    return
                raise WorktreeStateError(
                    f"cleanup terminal state {row.state} cannot become {expected}"
                )
            if (
                row.state != "cleanup_pending"
                or row.cleanup_owner != cleanup_owner
                or row.cleanup_epoch != cleanup_epoch
            ):
                raise WorktreeStateError("cleanup lease is no longer owned by the caller")
            if disposition == "removed":
                row.state = "cleaned"
                row.cleaned_at = func.clock_timestamp()
                row.cleanup_error = None
            elif disposition == "preserved":
                row.state = "preserved"
                row.cleanup_error = None
            else:
                if not error:
                    raise ValueError("failed cleanup requires an error")
                row.state = "cleanup_failed"
                row.cleanup_error = error
            row.cleanup_owner = None
            row.cleanup_lease_expires_at = None
            row.updated_at = func.clock_timestamp()

    async def recoverable(self) -> tuple[WorkspaceWorktree, ...]:
        async with self._sessions() as session:
            rows = await session.scalars(
                select(WorkspaceWorktree)
                .where(
                    WorkspaceWorktree.state.in_(
                        (
                            "preparing",
                            "ready",
                            "in_use",
                            "cleanup_pending",
                            "cleanup_failed",
                        )
                    )
                )
                .order_by(WorkspaceWorktree.created_at, WorkspaceWorktree.worktree_id)
            )
            return tuple(rows)

    async def _transition(
        self,
        execution_id: str,
        allowed: set[str],
        target: str,
        *,
        ready: bool = False,
    ) -> None:
        async with self._sessions() as session, session.begin():
            row = await self._load_by_execution(session, execution_id)
            if row.state not in allowed:
                raise WorktreeStateError(f"cannot move worktree from {row.state} to {target}")
            row.state = target
            row.updated_at = func.clock_timestamp()
            if ready and row.ready_at is None:
                row.ready_at = func.clock_timestamp()

    @staticmethod
    async def _load_by_execution(
        session: AsyncSession,
        execution_id: str,
    ) -> WorkspaceWorktree:
        row = await session.scalar(
            select(WorkspaceWorktree)
            .where(WorkspaceWorktree.execution_id == execution_id)
            .with_for_update()
        )
        if row is None:
            raise WorktreeStateError(f"unknown execution worktree {execution_id!r}")
        return row

    @staticmethod
    async def _database_now(session: AsyncSession) -> datetime:
        now = await session.scalar(select(func.clock_timestamp()))
        if not isinstance(now, datetime):
            raise WorktreeStateError("database did not return a cleanup lease timestamp")
        return now
