from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from multi_agent_v2.packages.domain.json_types import JsonValue


class CompilationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    path: str
    message: str
    context: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowCompilationError(ValueError):
    def __init__(self, issues: list[CompilationIssue] | tuple[CompilationIssue, ...]) -> None:
        ordered = tuple(sorted(issues, key=lambda issue: (issue.path, issue.code, issue.message)))
        if not ordered:
            raise ValueError("WorkflowCompilationError requires at least one issue")
        self.issues = ordered
        super().__init__("; ".join(f"{issue.path}: {issue.message}" for issue in ordered))


def issue(code: str, path: str, message: str, **context: JsonValue) -> CompilationIssue:
    return CompilationIssue(code=code, path=path, message=message, context=context)
