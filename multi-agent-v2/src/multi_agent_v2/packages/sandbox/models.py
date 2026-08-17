from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

Enforcement = Literal["full", "partial", "unavailable"]
ProcessSupervision = Literal["supervised", "sdk_managed", "unknown"]


class SandboxModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SandboxAttestation(SandboxModel):
    filesystem: Enforcement
    network: Enforcement
    process_tree: ProcessSupervision
    backend: str
    effective_policy: str
    limitations: tuple[str, ...] = ()


class SandboxRequirements(SandboxModel):
    filesystem: Enforcement = "partial"
    network: Enforcement = "unavailable"
    supervised_process: bool = False


class SandboxAdmissionError(RuntimeError):
    code = "sandbox.requirement_unsatisfied"


def require_sandbox(
    attestation: SandboxAttestation,
    requirements: SandboxRequirements,
) -> None:
    levels = {"unavailable": 0, "partial": 1, "full": 2}
    if levels[attestation.filesystem] < levels[requirements.filesystem]:
        raise SandboxAdmissionError(
            f"{requirements.filesystem} filesystem enforcement is unavailable"
        )
    if levels[attestation.network] < levels[requirements.network]:
        raise SandboxAdmissionError(f"{requirements.network} network enforcement is unavailable")
    if requirements.supervised_process and attestation.process_tree != "supervised":
        raise SandboxAdmissionError("platform-supervised process lifecycle is unavailable")
