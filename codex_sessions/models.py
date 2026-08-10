from pydantic import BaseModel, ConfigDict


class SessionSummary(BaseModel):
    """The public representation of one local Codex session."""

    model_config = ConfigDict(extra="forbid")

    name: str
    id: str
