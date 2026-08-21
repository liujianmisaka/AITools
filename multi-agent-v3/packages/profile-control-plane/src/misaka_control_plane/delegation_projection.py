from __future__ import annotations

from typing import Protocol

from misaka_delegation_capability import (
    DelegationGatewayPort,
    DelegationStore,
    DelegationUnauthorized,
)
from misaka_delegation_contracts import DelegationSnapshot
from misaka_interaction_contracts import PrincipalRef


class DelegationProjectionPort(Protocol):
    """Actor-aware read projection used by application transports."""

    async def list_visible(
        self,
        actor: PrincipalRef,
    ) -> tuple[DelegationSnapshot, ...]: ...


class StoreBackedDelegationProjection(DelegationProjectionPort):
    """List durable delegation snapshots without exposing store details."""

    def __init__(
        self,
        store: DelegationStore,
        gateway: DelegationGatewayPort,
    ) -> None:
        self._store = store
        self._gateway = gateway

    async def list_visible(
        self,
        actor: PrincipalRef,
    ) -> tuple[DelegationSnapshot, ...]:
        visible: list[DelegationSnapshot] = []
        for candidate in await self._store.list():
            try:
                visible.append(await self._gateway.get(candidate.ref.delegation_id, actor))
            except DelegationUnauthorized:
                continue
        return tuple(visible)
