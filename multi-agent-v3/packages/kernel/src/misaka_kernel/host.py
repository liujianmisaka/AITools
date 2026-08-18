from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Protocol

from misaka_kernel_contracts import (
    EventMode,
    ModuleManifest,
    RuntimeEvent,
    ServiceKey,
    ServiceProvision,
)

from misaka_kernel.errors import HostStateError, ModuleGraphError
from misaka_kernel.events import EventDispatcher
from misaka_kernel.lifecycle import AsyncDisposer, LifecycleScope
from misaka_kernel.registry import ProviderId, ServiceBinding, ServiceRegistry


class HostStatus(StrEnum):
    CREATED = "created"
    LOADING = "loading"
    ACTIVE = "active"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


class Module(Protocol):
    @property
    def manifest(self) -> ModuleManifest: ...

    async def attach(self, context: HostContext) -> AsyncDisposer | None: ...

    async def start(self, context: HostContext) -> None: ...


class HostContext:
    def __init__(self, host: Host, scope: LifecycleScope) -> None:
        self._host = host
        self._scope = scope

    @property
    def scope_name(self) -> str:
        return self._scope.name

    def require(self, key: ServiceKey) -> object:
        return self._host.services.resolve(key, name=self._host.bindings.get(key))

    def optional(self, key: ServiceKey) -> object | None:
        binding_name = self._host.bindings.get(key)
        if binding_name is not None:
            return self._host.services.require_named(key, binding_name)
        return self._host.services.optional(key)

    def require_named(self, key: ServiceKey, name: str) -> object:
        return self._host.services.require_named(key, name)

    def provide(
        self,
        key: ServiceKey,
        value: object,
        *,
        version: str,
        provider_id: str,
        name: str | None = None,
    ) -> None:
        self._host.services.register(
            ServiceBinding(
                key=key,
                value=value,
                version=version,
                provider_id=ProviderId(provider_id),
                name=name,
            )
        )

    def child_scope(self, name: str) -> LifecycleScope:
        return self._scope.child(name)

    def on(
        self,
        event_name: str,
        handler: object,
        *,
        mode: EventMode = EventMode.EMIT,
    ) -> AsyncDisposer:
        unsubscribe = self._host.events.on(event_name, handler, mode=mode)  # type: ignore[arg-type]

        async def dispose_subscription() -> None:
            unsubscribe()

        return self._scope.add(dispose_subscription)

    async def emit(self, event: RuntimeEvent) -> None:
        await self._host.events.emit(event)


class Host:
    def __init__(
        self,
        *,
        name: str = "host",
        bindings: Mapping[ServiceKey, str] | None = None,
    ) -> None:
        if not name.strip():
            raise HostStateError("host.name_empty", "host name must not be empty")
        self.name = name
        self.bindings = dict(bindings or {})
        self.status = HostStatus.CREATED
        self.services = ServiceRegistry()
        self.events = EventDispatcher()
        self._scope = LifecycleScope(name)
        self._modules: list[Module] = []
        self._context = HostContext(self, self._scope)

    def add_module(self, module: Module) -> None:
        if self.status is not HostStatus.CREATED:
            raise HostStateError(
                "host.module_after_start",
                "modules can only be added before start",
            )
        if any(
            existing.manifest.module_id == module.manifest.module_id
            for existing in self._modules
        ):
            raise ModuleGraphError(
                "module.duplicate",
                f"module {module.manifest.module_id} is already registered",
            )
        self._modules.append(module)

    async def start(self) -> None:
        if self.status is HostStatus.ACTIVE:
            return
        if self.status is not HostStatus.CREATED:
            raise HostStateError("host.start_invalid", f"cannot start host in {self.status} state")
        self.status = HostStatus.LOADING
        try:
            ordered = _order_modules(self._modules)
            for module in ordered:
                disposer = await module.attach(self._context)
                if disposer is not None:
                    self._scope.add(disposer)
                await module.start(self._context)
        except Exception:
            self.status = HostStatus.FAILED
            try:
                await self._scope.close()
            finally:
                self.services.clear()
            raise
        self.status = HostStatus.ACTIVE

    async def stop(self) -> None:
        if self.status is HostStatus.STOPPED:
            return
        if self.status is HostStatus.CREATED:
            self.status = HostStatus.STOPPED
            return
        self.status = HostStatus.DRAINING
        try:
            await self._scope.close()
        finally:
            self.services.clear()
            self.status = HostStatus.STOPPED


def _order_modules(modules: Iterable[Module]) -> tuple[Module, ...]:
    values = tuple(modules)
    providers: dict[ServiceKey, list[Module]] = {}
    by_id = {module.manifest.module_id: module for module in values}
    for module in values:
        for provision in module.manifest.provides:
            providers.setdefault(provision.key, []).append(module)
    for module in values:
        for conflict in module.manifest.conflicts:
            if conflict in by_id:
                raise ModuleGraphError(
                    "module.conflict",
                    f"module {module.manifest.module_id} conflicts with {conflict}",
                )

    edges: dict[object, set[object]] = {
        module.manifest.module_id: set() for module in values
    }
    for module in values:
        for requirement in module.manifest.requires:
            dependencies = [
                provider
                for provider in providers.get(requirement.key, [])
                if _version_matches(
                    provider.manifest.provides,
                    requirement.key,
                    requirement.version,
                )
            ]
            if not dependencies:
                if requirement.optional:
                    continue
                raise ModuleGraphError(
                    "module.service_missing",
                    f"module {module.manifest.module_id} requires {requirement.key}",
                )
            edges[module.manifest.module_id].update(
                dependency.manifest.module_id
                for dependency in dependencies
                if dependency is not module
            )

    result: list[Module] = []
    remaining = set(by_id)
    while remaining:
        ready = sorted(
            (module_id for module_id in remaining if not (edges[module_id] & remaining)),
            key=str,
        )
        if not ready:
            cycle = ", ".join(str(module_id) for module_id in remaining)
            raise ModuleGraphError("module.cycle", f"module dependency cycle: {cycle}")
        result.extend(by_id[module_id] for module_id in ready)
        remaining.difference_update(ready)
    return tuple(result)


def _version_matches(
    provisions: tuple[ServiceProvision, ...],
    key: ServiceKey,
    required_version: str | None,
) -> bool:
    if required_version is None:
        return True
    return any(
        provision.key == key and provision.version == required_version
        for provision in provisions
    )
