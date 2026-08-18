from __future__ import annotations

from misaka_invocation_contracts import (
    CapabilityDescriptor,
    CapabilityFeature,
    CapabilityOperation,
)
from misaka_kernel_contracts import ServiceKey

AGENT_CAPABILITY_ID = "agent.invocation"
AGENT_OPERATION_INVOKE = "invoke"
AGENT_PROVIDER_SERVICE = ServiceKey("capability.agent.provider")


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
