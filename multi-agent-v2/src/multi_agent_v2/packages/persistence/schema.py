CURRENT_SCHEMA_REVISION = "0001_phase1_baseline"


class DatabaseSchemaError(RuntimeError):
    """Raised when the Control DB is not migrated to the application revision."""
