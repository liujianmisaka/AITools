from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from misaka_kernel_contracts import (
    EventDeclaration,
    EventMode,
    JsonObject,
    ModuleId,
    ModuleManifest,
    RuntimeEvent,
    ServiceKey,
    ServiceProvision,
    ServiceShape,
)

from misaka_kernel.errors import HostStateError, ModuleGraphError
from misaka_kernel.events import EventDispatcher, EventDispatchResult, EventHandlerLike
from misaka_kernel.lifecycle import AsyncDisposer, LifecycleScope
from misaka_kernel.registry import ProviderId, ServiceBinding, ServiceRegistry

if TYPE_CHECKING:
    from misaka_kernel.profile import CompositionSnapshot


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
    def __init__(
        self,
        host: Host,
        scope: LifecycleScope,
        manifest: ModuleManifest,
        configuration: JsonObject,
    ) -> None:
        self._host = host
        self._scope = scope
        self._manifest = manifest
        self._configuration = dict(configuration)

    @property
    def scope_name(self) -> str:
        return self._scope.name

    @property
    def configuration(self) -> JsonObject:
        return dict(self._configuration)

    def require(self, key: ServiceKey) -> object:
        return self._host.services.resolve(
            key,
            name=self._host.bindings.get(key),
            scope_id=self._scope.name,
        )

    def optional(self, key: ServiceKey) -> object | None:
        binding_name = self._host.bindings.get(key)
        if binding_name is not None:
            return self._host.services.require_named(key, binding_name)
        return self._host.services.optional(key, scope_id=self._scope.name)

    def require_named(self, key: ServiceKey, name: str) -> object:
        return self._host.services.require_named(key, name)

    def provide(
        self,
        key: ServiceKey,
        value: object,
        *,
        version: str,
        name: str | None = None,
    ) -> AsyncDisposer:
        provisions = [
            provision
            for provision in self._manifest.provides
            if provision.key == key and provision.name == name
        ]
        if not provisions:
            raise ModuleGraphError(
                "module.service_undeclared",
                f"module {self._manifest.module_id} did not declare service {key}",
            )
        provision = provisions[0]
        if provision.version != version:
            raise ModuleGraphError(
                "module.service_version_mismatch",
                f"module {self._manifest.module_id} declared {key} version "
                f"{provision.version} but registered {version}",
            )
        binding = ServiceBinding(
            key=key,
            value=value,
            version=version,
            provider_id=ProviderId(str(self._manifest.module_id)),
            shape=provision.shape,
            name=name,
        )
        self._host.services.register(binding)
        disposed = False

        async def dispose() -> None:
            nonlocal disposed
            if disposed:
                return
            disposed = True
            self._host.services.unregister(binding)

        return self._scope.add(dispose)

    def child_scope(self, name: str) -> LifecycleScope:
        return self._scope.child(name)

    def child_context(self, name: str) -> HostContext:
        child = self._scope.child(name)
        self.register_scope_cleanup(child)
        return HostContext(
            self._host,
            child,
            self._manifest,
            self._configuration,
        )

    def on(
        self,
        event_name: str,
        handler: object,
        *,
        mode: EventMode = EventMode.EMIT,
        scope_id: str = "*",
    ) -> AsyncDisposer:
        unsubscribe = self._host.events.on(
            event_name,
            cast(EventHandlerLike, handler),
            mode=mode,
            consumer_id=str(self._manifest.module_id),
            scope_id=scope_id,
        )

        async def dispose_subscription() -> None:
            unsubscribe()

        return self._scope.add(dispose_subscription)

    async def emit(self, event: RuntimeEvent) -> EventDispatchResult:
        return await self._host.events.dispatch(event)

    def declare_event(self, declaration: EventDeclaration) -> AsyncDisposer:
        if declaration.producer != str(self._manifest.module_id):
            raise ModuleGraphError(
                "event.producer_owner_mismatch",
                f"module {self._manifest.module_id} cannot declare producer {declaration.producer}",
            )
        remove = self._host.events.declare(declaration)

        async def dispose_declaration() -> None:
            remove()

        return self._scope.add(dispose_declaration)

    def register_scope_cleanup(self, scope: LifecycleScope) -> None:
        async def clear_scope() -> None:
            self._host.services.clear_scope(scope.name)

        scope.add(clear_scope)


class Host:
    def __init__(
        self,
        *,
        name: str = "host",
        bindings: Mapping[ServiceKey, str] | None = None,
        configurations: Mapping[ModuleId, JsonObject] | None = None,
        composition_snapshot: CompositionSnapshot | None = None,
    ) -> None:
        if not name.strip():
            raise HostStateError("host.name_empty", "host name must not be empty")
        self.name = name
        self.composition_snapshot = composition_snapshot
        self.bindings = dict(bindings or {})
        self.configurations = {
            module_id: dict(configuration)
            for module_id, configuration in (configurations or {}).items()
        }
        self.status = HostStatus.CREATED
        self.services = ServiceRegistry()
        self.events = EventDispatcher()
        self._scope = LifecycleScope(name)
        self._modules: list[Module] = []

    def add_module(self, module: Module) -> None:
        if self.status is not HostStatus.CREATED:
            raise HostStateError(
                "host.module_after_start",
                "modules can only be added before start",
            )
        if any(
            existing.manifest.module_id == module.manifest.module_id for existing in self._modules
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
            ordered = _order_modules(self._modules, self.bindings)
            for module in ordered:
                module_scope = self._scope.child(str(module.manifest.module_id))
                context = HostContext(
                    self,
                    module_scope,
                    module.manifest,
                    self.configurations.get(module.manifest.module_id, {}),
                )
                context.register_scope_cleanup(module_scope)
                disposer = await module.attach(context)
                if disposer is not None:
                    module_scope.add(disposer)
                _validate_module_provisions(module.manifest, self.services)
                await module.start(context)
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


type ProviderDeclaration = tuple[Module, ServiceProvision]


def _order_modules(
    modules: Iterable[Module],
    bindings: Mapping[ServiceKey, str],
) -> tuple[Module, ...]:
    values = tuple(modules)
    providers: dict[ServiceKey, list[ProviderDeclaration]] = {}
    by_id = {module.manifest.module_id: module for module in values}
    for module in values:
        for provision in module.manifest.provides:
            providers.setdefault(provision.key, []).append((module, provision))
    _validate_provider_shapes(providers)
    _validate_profile_bindings(providers, bindings)
    for module in values:
        for conflict in module.manifest.conflicts:
            if conflict in by_id:
                raise ModuleGraphError(
                    "module.conflict",
                    f"module {module.manifest.module_id} conflicts with {conflict}",
                )

    edges: dict[ModuleId, set[ModuleId]] = {module.manifest.module_id: set() for module in values}
    for module in values:
        for requirement in module.manifest.requires:
            dependencies = _select_dependencies(
                module,
                requirement.key,
                requirement.version,
                providers,
                bindings,
                optional=False,
            )
            edges[module.manifest.module_id].update(
                dependency.manifest.module_id
                for dependency in dependencies
                if dependency is not module
            )
        for requirement in module.manifest.optional_requires:
            dependencies = _select_dependencies(
                module,
                requirement.key,
                requirement.version,
                providers,
                bindings,
                optional=True,
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


def _select_dependencies(
    consumer: Module,
    key: ServiceKey,
    required_version: str | None,
    providers: Mapping[ServiceKey, list[ProviderDeclaration]],
    bindings: Mapping[ServiceKey, str],
    *,
    optional: bool,
) -> tuple[Module, ...]:
    declarations = [
        declaration
        for declaration in providers.get(key, [])
        if required_version is None or declaration[1].version == required_version
    ]
    selected_name = bindings.get(key)
    if selected_name is not None:
        declarations = [
            declaration for declaration in declarations if declaration[1].name == selected_name
        ]
    else:
        unnamed = [
            declaration
            for declaration in declarations
            if declaration[1].shape is not ServiceShape.NAMED
        ]
        if unnamed:
            declarations = unnamed
        elif declarations:
            if optional:
                return ()
            raise ModuleGraphError(
                "module.service_binding_required",
                f"module {consumer.manifest.module_id} requires an explicit binding for {key}",
            )
    if not declarations:
        if optional:
            return ()
        version_suffix = f" version {required_version}" if required_version is not None else ""
        raise ModuleGraphError(
            "module.service_missing",
            f"module {consumer.manifest.module_id} requires {key}{version_suffix}",
        )
    return tuple(declaration[0] for declaration in declarations)


def _validate_provider_shapes(
    providers: Mapping[ServiceKey, list[ProviderDeclaration]],
) -> None:
    for key, declarations in providers.items():
        shapes = {provision.shape for _, provision in declarations}
        if len(shapes) > 1:
            raise ModuleGraphError(
                "service.shape_conflict",
                f"service {key} mixes incompatible provider shapes",
            )
        shape = next(iter(shapes))
        if shape is not ServiceShape.NAMED and len(declarations) > 1:
            raise ModuleGraphError(
                "service.provider_duplicate",
                f"service {key} has multiple non-named providers",
            )
        names = [provision.name for _, provision in declarations]
        if len(names) != len(set(names)):
            raise ModuleGraphError(
                "service.named_duplicate",
                f"service {key} has duplicate named providers",
            )


def _validate_profile_bindings(
    providers: Mapping[ServiceKey, list[ProviderDeclaration]],
    bindings: Mapping[ServiceKey, str],
) -> None:
    for key, selected_name in bindings.items():
        declarations = providers.get(key, [])
        if not any(
            provision.shape is ServiceShape.NAMED and provision.name == selected_name
            for _, provision in declarations
        ):
            raise ModuleGraphError(
                "profile.binding_missing",
                f"profile binding {key}={selected_name} has no matching provider",
            )


def _validate_module_provisions(
    manifest: ModuleManifest,
    registry: ServiceRegistry,
) -> None:
    provider_id = ProviderId(str(manifest.module_id))
    for provision in manifest.provides:
        if not registry.has_binding(
            provision.key,
            provider_id=provider_id,
            version=provision.version,
            shape=provision.shape,
            name=provision.name,
        ):
            raise ModuleGraphError(
                "module.service_not_registered",
                f"module {manifest.module_id} did not register declared service {provision.key}",
            )
