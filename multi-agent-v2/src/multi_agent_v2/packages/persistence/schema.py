CURRENT_SCHEMA_REVISION = "0004_phase7_execution_evidence"


class DatabaseSchemaError(RuntimeError):
    """Raised when the Control DB is not migrated to the application revision."""
