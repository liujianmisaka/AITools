CURRENT_SCHEMA_REVISION = "0003_phase4_control_plane"


class DatabaseSchemaError(RuntimeError):
    """Raised when the Control DB is not migrated to the application revision."""
