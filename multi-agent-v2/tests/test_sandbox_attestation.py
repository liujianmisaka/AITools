from __future__ import annotations

import pytest

from multi_agent_v2.packages.sandbox import (
    SandboxAdmissionError,
    SandboxAttestation,
    SandboxRequirements,
    require_sandbox,
)


def test_full_requirements_accept_a_fully_enforced_runtime() -> None:
    require_sandbox(
        SandboxAttestation(
            filesystem="full",
            network="full",
            process_tree="supervised",
            backend="test",
            effective_policy="read_only",
        ),
        SandboxRequirements(
            filesystem="full",
            network="full",
            supervised_process=True,
        ),
    )


@pytest.mark.parametrize(
    ("attestation", "message"),
    [
        (
            SandboxAttestation(
                filesystem="partial",
                network="full",
                process_tree="supervised",
                backend="test",
                effective_policy="read_only",
            ),
            "filesystem",
        ),
        (
            SandboxAttestation(
                filesystem="full",
                network="partial",
                process_tree="supervised",
                backend="test",
                effective_policy="read_only",
            ),
            "network",
        ),
        (
            SandboxAttestation(
                filesystem="full",
                network="full",
                process_tree="sdk_managed",
                backend="test",
                effective_policy="read_only",
            ),
            "process",
        ),
    ],
)
def test_requirements_fail_closed(
    attestation: SandboxAttestation,
    message: str,
) -> None:
    with pytest.raises(SandboxAdmissionError, match=message):
        require_sandbox(
            attestation,
            SandboxRequirements(
                filesystem="full",
                network="full",
                supervised_process=True,
            ),
        )


def test_partial_network_requirement_still_rejects_unavailable_enforcement() -> None:
    with pytest.raises(SandboxAdmissionError, match="network"):
        require_sandbox(
            SandboxAttestation(
                filesystem="partial",
                network="unavailable",
                process_tree="sdk_managed",
                backend="test",
                effective_policy="read_only",
            ),
            SandboxRequirements(network="partial"),
        )


def test_unrestricted_network_policy_accepts_an_unavailable_network_boundary() -> None:
    require_sandbox(
        SandboxAttestation(
            filesystem="partial",
            network="unavailable",
            process_tree="sdk_managed",
            backend="test",
            effective_policy="workspace_write",
        ),
        SandboxRequirements(filesystem="partial", network="unavailable"),
    )
