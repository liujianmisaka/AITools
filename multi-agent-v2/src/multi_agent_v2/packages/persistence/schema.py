CURRENT_SCHEMA_REVISION = "0002_phase3_agent_execution"


class DatabaseSchemaError(RuntimeError):
    """Raised when the Control DB is not migrated to the application revision."""
