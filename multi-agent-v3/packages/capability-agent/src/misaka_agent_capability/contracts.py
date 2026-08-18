from __future__ import annotations

from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    CapabilityOperation,
)

AGENT_CAPABILITY_ID = "agent.invocation"
AGENT_OPERATION_INVOKE = "invoke"


def agent_descriptor(
    *,
    features: frozenset[CapabilityFeature] = frozenset(),
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=AGENT_CAPABILITY_ID,
        version="1.0.0",
        operations=(CapabilityOperation(name=AGENT_OPERATION_INVOKE),),
        features=features,
    )
