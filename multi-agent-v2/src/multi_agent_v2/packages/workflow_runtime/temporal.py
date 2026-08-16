from __future__ import annotations

import asyncio

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client


class TemporalGateway:
    """Owns the process-wide Temporal client and exposes a narrow health boundary."""

    def __init__(self, *, address: str, namespace: str) -> None:
        self._address = address
        self._namespace = namespace
        self._client: Client | None = None
        self._connect_lock = asyncio.Lock()

    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("Temporal client is not connected")
        return self._client

    async def connect(self) -> Client:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is None:
                self._client = await Client.connect(
                    self._address,
                    namespace=self._namespace,
                )
        return self._client

    async def check(self) -> None:
        client = await self.connect()
        healthy = await client.service_client.check_health()
        if not healthy:
            raise RuntimeError("Temporal health service reported unavailable")
        await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=self._namespace)
        )


class TemporalProbe:
    name = "temporal"

    def __init__(self, gateway: TemporalGateway) -> None:
        self._gateway = gateway

    async def check(self) -> None:
        await self._gateway.check()
