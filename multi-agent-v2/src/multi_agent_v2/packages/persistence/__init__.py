"""PostgreSQL persistence boundary."""

from multi_agent_v2.packages.persistence.agent_models import (
    AgentExecutionAttempt,
    AgentExecutionLease,
    ProviderSession,
    WorkspaceWorktree,
)
from multi_agent_v2.packages.persistence.database import DatabaseManager, DatabaseProbe
from multi_agent_v2.packages.persistence.execution_lease import (
    ExecutionAttemptRegistration,
    ExecutionIdentityConflict,
    ExecutionLeaseError,
    ExecutionLeaseLost,
    ExecutionLeaseRepository,
    ExecutionRegistration,
    ExecutionStateConflict,
    LeaseClaimDisposition,
    LeaseClaimResult,
    ProviderSessionBusy,
    ProviderSessionClaim,
)
from multi_agent_v2.packages.persistence.schema import (
    CURRENT_SCHEMA_REVISION,
    DatabaseSchemaError,
)
from multi_agent_v2.packages.persistence.worktree_repository import (
    CleanupClaimDisposition,
    WorktreeCleanupClaim,
    WorktreeRegistration,
    WorktreeRepository,
    WorktreeStateError,
)

__all__ = [
    "CURRENT_SCHEMA_REVISION",
    "AgentExecutionAttempt",
    "AgentExecutionLease",
    "CleanupClaimDisposition",
    "DatabaseManager",
    "DatabaseProbe",
    "DatabaseSchemaError",
    "ExecutionAttemptRegistration",
    "ExecutionIdentityConflict",
    "ExecutionLeaseError",
    "ExecutionLeaseLost",
    "ExecutionLeaseRepository",
    "ExecutionRegistration",
    "ExecutionStateConflict",
    "LeaseClaimDisposition",
    "LeaseClaimResult",
    "ProviderSession",
    "ProviderSessionBusy",
    "ProviderSessionClaim",
    "WorkspaceWorktree",
    "WorktreeCleanupClaim",
    "WorktreeRegistration",
    "WorktreeRepository",
    "WorktreeStateError",
]
