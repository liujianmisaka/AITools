from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from misaka_kernel import (
    BailDispatchResult,
    EventDeclarationError,
    EventDispatcher,
    Host,
    HostContext,
    HostStatus,
    LifecycleScope,
    ModuleGraphError,
    ProfileDefinition,
    ProfileLoader,
    ServiceBinding,
    ServiceRegistry,
    WaterfallDispatchResult,
)
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel.registry import ProviderId
from misaka_kernel_contracts import (
    EventDeclaration,
    EventFailureIsolation,
    EventMode,
    ModuleId,
    ModuleManifest,
    RuntimeEvent,
    ServiceKey,
    ServiceProvision,
    ServiceRequirement,
    ServiceShape,
)


@dataclass
class _Module:
    module_id: str
    requires: tuple[ServiceRequirement, ...] = ()
    provides: tuple[ServiceProvision, ...] = ()
    log: list[str] | None = None
    fail_start: bool = False

    @property
    def manifest(self) -> ModuleManifest:
        return ModuleManifest(
            module_id=ModuleId(self.module_id),
            version="1.0.0",
            requires=self.requires,
            provides=self.provides,
        )

    async def attach(self, context: HostContext) -> AsyncDisposer | None:
        if self.log is not None:
            self.log.append(f"attach:{self.module_id}")
        for provision in self.provides:
            context.provide(
                provision.key,
                f"value:{self.module_id}",
                version=provision.version,
                name=provision.name,
            )
        return None

    async def start(self, context: HostContext) -> None:
        if self.log is not None:
            self.log.append(f"start:{self.module_id}")
        if self.fail_start:
            raise RuntimeError(self.module_id)


def _provision(
    key: str,
    *,
    name: str | None = None,
    shape: ServiceShape = ServiceShape.SINGLETON,
) -> ServiceProvision:
    return ServiceProvision(ServiceKey(key), "1.0.0", shape=shape, name=name)


def test_service_registry_requires_explicit_named_provider() -> None:
    registry = ServiceRegistry()
    registry.register(ServiceBinding(ServiceKey("agent"), object(), "1.0.0", ProviderId("fake")))
    assert registry.require(ServiceKey("agent")) is not None

    registry.register(
        ServiceBinding(
            ServiceKey("tool"),
            "a",
            "1.0.0",
            ProviderId("tool-a"),
            shape=ServiceShape.NAMED,
            name="a",
        )
    )
    with pytest.raises(Exception, match="named providers"):
        registry.require(ServiceKey("tool"))
    assert registry.require_named(ServiceKey("tool"), "a") == "a"


def test_service_registry_unregistration_is_identity_fenced() -> None:
    registry = ServiceRegistry()
    original = ServiceBinding(ServiceKey("agent"), "original", "1.0.0", ProviderId("a"))
    replacement = ServiceBinding(ServiceKey("agent"), "replacement", "1.0.0", ProviderId("b"))
    registry.register(original)
    registry.unregister(original)
    registry.register(replacement)

    registry.unregister(original)
    assert registry.require(ServiceKey("agent")) == "replacement"
    registry.unregister(replacement)
    assert registry.snapshot() == ()


@pytest.mark.asyncio
async def test_host_context_provide_returns_idempotent_disposer() -> None:
    class DisposableProvider(_Module):
        def __init__(self) -> None:
            super().__init__("disposable", provides=(_provision("agent"),))
            self.disposer: AsyncDisposer | None = None

        async def attach(self, context: HostContext) -> AsyncDisposer | None:
            self.disposer = context.provide(
                ServiceKey("agent"),
                "value",
                version="1.0.0",
            )
            return None

    provider = DisposableProvider()
    host = Host()
    host.add_module(provider)
    await host.start()
    assert host.services.require(ServiceKey("agent")) == "value"
    assert provider.disposer is not None

    await provider.disposer()
    await provider.disposer()
    assert host.services.snapshot() == ()
    await host.stop()


def test_profile_loader_selects_explicit_named_binding() -> None:
    class NamedModule(_Module):
        async def attach(self, context: HostContext) -> AsyncDisposer | None:
            assert context is not None
            return None

        async def start(self, context: HostContext) -> None:
            return None

    profile = ProfileDefinition(
        profile_id="named-profile",
        module_ids=(ModuleId("named"),),
        bindings={ServiceKey("tool"): "a"},
    )
    host = ProfileLoader({ModuleId("named"): lambda: NamedModule("named")}).create_host(profile)
    assert host.name == "named-profile"
    assert host.composition_snapshot is not None
    assert host.composition_snapshot.profile_id == "named-profile"


def test_profile_snapshot_is_deterministic_and_records_ownership() -> None:
    profile = ProfileDefinition(
        profile_id="snapshot-profile",
        module_ids=(ModuleId("one"), ModuleId("two")),
        transport_ids=("http", "cli"),
        fact_owners={"execution": "runtime"},
        projection_sources={"jobs": "execution"},
        resource_owners={"workspace": "execution"},
    )
    loader = ProfileLoader(
        {
            ModuleId("one"): lambda: _Module("one"),
            ModuleId("two"): lambda: _Module("two"),
        }
    )
    first = loader.snapshot(profile)
    second = loader.snapshot(profile)
    assert first == second
    assert first.composition_hash
    assert first.transport_ids == ("http", "cli")
    assert first.fact_owners == (("execution", "runtime"),)
    assert first.projection_sources == (("jobs", "execution"),)
    assert first.resource_owners == (("workspace", "execution"),)


def test_profile_loader_rejects_unknown_module() -> None:
    profile = ProfileDefinition(profile_id="missing-profile", module_ids=(ModuleId("missing"),))
    with pytest.raises(ModuleGraphError, match="unavailable"):
        ProfileLoader({}).create_host(profile)


@pytest.mark.asyncio
async def test_host_rejects_incompatible_required_service_version() -> None:
    host = Host()
    host.add_module(_Module("provider", provides=(_provision("agent"),)))
    host.add_module(
        _Module(
            "consumer",
            requires=(ServiceRequirement(ServiceKey("agent"), version="2.0.0"),),
        )
    )
    with pytest.raises(ModuleGraphError, match="requires"):
        await host.start()


@pytest.mark.asyncio
async def test_host_orders_modules_and_isolates_hosts() -> None:
    log: list[str] = []
    provider = _Module("provider", provides=(_provision("agent"),), log=log)
    consumer = _Module(
        "consumer",
        requires=(ServiceRequirement(ServiceKey("agent")),),
        log=log,
    )
    first = Host(name="first")
    first.add_module(consumer)
    first.add_module(provider)
    second = Host(name="second")

    await first.start()
    assert first.status is HostStatus.ACTIVE
    assert log == ["attach:provider", "start:provider", "attach:consumer", "start:consumer"]
    await second.start()
    assert second.services.snapshot() == ()
    await first.stop()
    await second.stop()


@pytest.mark.asyncio
async def test_host_rejects_missing_service_and_rolls_back() -> None:
    host = Host()
    host.add_module(_Module("consumer", requires=(ServiceRequirement(ServiceKey("missing")),)))
    with pytest.raises(ModuleGraphError, match="requires"):
        await host.start()
    assert host.status is HostStatus.FAILED


@pytest.mark.asyncio
async def test_host_requires_module_to_register_declared_service() -> None:
    class SilentProvider(_Module):
        async def attach(self, context: HostContext) -> AsyncDisposer | None:
            return None

    host = Host()
    host.add_module(SilentProvider("provider", provides=(_provision("agent"),)))

    with pytest.raises(ModuleGraphError, match="did not register"):
        await host.start()

    assert host.status is HostStatus.FAILED


@pytest.mark.asyncio
async def test_host_requires_explicit_binding_for_named_services() -> None:
    host = Host()
    host.add_module(
        _Module(
            "provider-a",
            provides=(_provision("tool", name="a", shape=ServiceShape.NAMED),),
        )
    )
    host.add_module(
        _Module(
            "consumer",
            requires=(ServiceRequirement(ServiceKey("tool")),),
        )
    )

    with pytest.raises(ModuleGraphError, match="explicit binding"):
        await host.start()


@pytest.mark.asyncio
async def test_host_resolves_scoped_service_per_scope() -> None:
    instances: list[str] = []

    class ScopedProvider(_Module):
        async def attach(self, context: HostContext) -> AsyncDisposer | None:
            def factory(scope_id: str) -> object:
                instances.append(scope_id)
                return object()

            context.provide(
                ServiceKey("scope"),
                factory,
                version="1.0.0",
            )
            return None

    class ScopedConsumer(_Module):
        async def attach(self, context: HostContext) -> AsyncDisposer | None:
            assert context.require(ServiceKey("scope")) is context.require(ServiceKey("scope"))
            return None

    host = Host()
    host.add_module(
        ScopedProvider(
            "provider",
            provides=(_provision("scope", shape=ServiceShape.SCOPED),),
        )
    )
    host.add_module(
        ScopedConsumer(
            "consumer",
            requires=(ServiceRequirement(ServiceKey("scope")),),
        )
    )

    await host.start()
    assert instances == ["consumer"]
    await host.stop()


def test_profile_validates_module_configuration() -> None:
    class ConfiguredModule(_Module):
        @property
        def manifest(self) -> ModuleManifest:
            return ModuleManifest(
                module_id=ModuleId("configured"),
                version="1.0.0",
                configuration_schema={
                    "type": "object",
                    "required": ["mode"],
                    "properties": {"mode": {"type": "string"}},
                    "additionalProperties": False,
                },
            )

    profile = ProfileDefinition(
        profile_id="configured-profile",
        module_ids=(ModuleId("configured"),),
        configurations={ModuleId("configured"): {"mode": "local"}},
    )
    host = ProfileLoader(
        {ModuleId("configured"): lambda: ConfiguredModule("configured")}
    ).create_host(profile)

    assert host.name == "configured-profile"


@pytest.mark.asyncio
async def test_host_start_failure_runs_disposers_and_stop_is_idempotent() -> None:
    log: list[str] = []
    host = Host()

    async def dispose_first() -> None:
        log.append("dispose:first")

    async def dispose_second() -> None:
        log.append("dispose:second")

    class DisposableModule(_Module):
        async def attach(self, context: object) -> AsyncDisposer:
            log.append(f"attach:{self.module_id}")
            return dispose_first if self.module_id == "first" else dispose_second

    host.add_module(DisposableModule("first", log=log))
    host.add_module(DisposableModule("second", log=log, fail_start=True))
    with pytest.raises(RuntimeError, match="second"):
        await host.start()
    assert log[-2:] == ["dispose:second", "dispose:first"]
    assert host.status is HostStatus.FAILED
    await host.stop()
    await host.stop()
    assert host.status is HostStatus.STOPPED


@pytest.mark.asyncio
async def test_event_dispatch_isolates_emit_failures_and_preserves_order() -> None:
    dispatcher = EventDispatcher()
    dispatcher.declare(
        EventDeclaration(
            event_name="test",
            producer="kernel",
            payload_schema={"type": "object"},
        )
    )
    seen: list[str] = []

    async def failing(event: RuntimeEvent) -> None:
        seen.append("failing")
        raise RuntimeError("listener")

    async def succeeding(event: RuntimeEvent) -> None:
        seen.append(event.name)

    dispatcher.on("test", failing)
    dispatcher.on("test", succeeding)
    failures = await dispatcher.emit(RuntimeEvent(name="test"))
    assert seen == ["failing", "test"]
    assert len(failures) == 1


def test_event_declaration_disposer_is_idempotent_and_conflicts_are_rejected() -> None:
    dispatcher = EventDispatcher()
    declaration = EventDeclaration(event_name="test", producer="kernel")
    dispose = dispatcher.declare(declaration)
    assert dispatcher.declarations() == (declaration,)
    with pytest.raises(EventDeclarationError, match="already declared"):
        dispatcher.declare(declaration)
    dispose()
    dispose()
    assert dispatcher.declarations() == ()

    async def handler(event: RuntimeEvent) -> None:
        del event

    with pytest.raises(EventDeclarationError, match="must be declared"):
        dispatcher.on("test", handler)


@pytest.mark.asyncio
async def test_event_dispatch_validates_version_scope_schema_and_consumer() -> None:
    dispatcher = EventDispatcher()
    dispatcher.declare(
        EventDeclaration(
            event_name="scoped",
            version=2,
            payload_schema={
                "type": "object",
                "required": ["value"],
                "properties": {"value": {"type": "integer"}},
                "additionalProperties": False,
            },
            scope="scope-a",
            producer="module-a",
            consumer=("consumer-a",),
            failure_isolation=EventFailureIsolation.ISOLATE,
        )
    )
    seen: list[int] = []

    async def handler(event: RuntimeEvent) -> None:
        value = event.payload["value"]
        assert isinstance(value, int) and not isinstance(value, bool)
        seen.append(value)

    dispatcher.on("scoped", handler, consumer_id="consumer-a", scope_id="scope-a")
    dispatcher.on("scoped", handler, consumer_id="consumer-a", scope_id="scope-b")
    with pytest.raises(EventDeclarationError, match="not declared"):
        dispatcher.on("scoped", handler, consumer_id="consumer-b", scope_id="scope-a")
    await dispatcher.dispatch(
        RuntimeEvent(
            name="scoped",
            payload={"value": 3},
            source="module-a",
            version=2,
            scope_id="scope-a",
        )
    )
    assert seen == [3]

    with pytest.raises(EventDeclarationError, match="version"):
        await dispatcher.dispatch(
            RuntimeEvent(
                name="scoped",
                payload={"value": 3},
                source="module-a",
                version=1,
                scope_id="scope-a",
            )
        )
    with pytest.raises(EventDeclarationError, match="payload"):
        await dispatcher.dispatch(
            RuntimeEvent(
                name="scoped",
                payload={"value": "bad"},
                source="module-a",
                version=2,
                scope_id="scope-a",
            )
        )


@pytest.mark.asyncio
async def test_bail_event_stops_after_first_decision() -> None:
    dispatcher = EventDispatcher()
    dispatcher.declare(EventDeclaration(event_name="decision", mode=EventMode.BAIL))
    seen: list[str] = []

    async def abstain(event: RuntimeEvent) -> bool | None:
        del event
        seen.append("abstain")
        return None

    async def accept(event: RuntimeEvent) -> bool | None:
        del event
        seen.append("accept")
        return True

    async def late(event: RuntimeEvent) -> bool | None:
        del event
        seen.append("late")
        return False

    dispatcher.on("decision", abstain, mode=EventMode.BAIL)
    dispatcher.on("decision", accept, mode=EventMode.BAIL)
    dispatcher.on("decision", late, mode=EventMode.BAIL)
    result = await dispatcher.bail(RuntimeEvent(name="decision"))

    assert result == BailDispatchResult(decision=True)
    assert seen == ["abstain", "accept"]


@pytest.mark.asyncio
async def test_bail_event_isolates_failure_and_returns_later_decision() -> None:
    dispatcher = EventDispatcher()
    dispatcher.declare(EventDeclaration(event_name="decision", mode=EventMode.BAIL))

    async def failing(event: RuntimeEvent) -> bool | None:
        del event
        raise RuntimeError("broken decision handler")

    async def reject(event: RuntimeEvent) -> bool | None:
        del event
        return False

    dispatcher.on("decision", failing, mode=EventMode.BAIL)
    dispatcher.on("decision", reject, mode=EventMode.BAIL)
    result = await dispatcher.bail(RuntimeEvent(name="decision"))

    assert result.decision is False
    assert len(result.failures) == 1
    assert result.failures[0].handler_name.endswith("failing")


@pytest.mark.asyncio
async def test_waterfall_event_reports_isolated_failure() -> None:
    dispatcher = EventDispatcher()
    dispatcher.declare(EventDeclaration(event_name="rewrite", mode=EventMode.WATERFALL))

    async def failing(
        event: RuntimeEvent,
        next_handler: Callable[[], Awaitable[RuntimeEvent]],
    ) -> RuntimeEvent:
        del event, next_handler
        raise RuntimeError("broken waterfall handler")

    dispatcher.on("rewrite", failing, mode=EventMode.WATERFALL)
    event = RuntimeEvent(name="rewrite")
    result = await dispatcher.waterfall(event)

    assert isinstance(result, WaterfallDispatchResult)
    assert result.event == event
    assert len(result.failures) == 1
    assert result.failures[0].handler_name.endswith("failing")


@pytest.mark.asyncio
async def test_host_event_declarations_and_subscriptions_follow_module_lifecycle() -> None:
    seen: list[str] = []

    class EventModule(_Module):
        async def attach(self, context: HostContext) -> AsyncDisposer | None:
            context.declare_event(
                EventDeclaration(
                    event_name="module.ready",
                    producer="event-module",
                    consumer=("event-module",),
                    scope="module-scope",
                )
            )

            async def handle(event: RuntimeEvent) -> None:
                seen.append(event.scope_id)

            context.on("module.ready", handle, scope_id="module-scope")
            return None

        async def start(self, context: HostContext) -> None:
            await context.emit(
                RuntimeEvent(
                    name="module.ready",
                    source="event-module",
                    scope_id="module-scope",
                )
            )

    host = Host()
    host.add_module(EventModule("event-module"))
    await host.start()
    assert seen == ["module-scope"]
    assert [item.event_name for item in host.events.declarations()] == ["module.ready"]

    await host.stop()
    assert host.events.declarations() == ()


@pytest.mark.asyncio
async def test_lifecycle_scope_closes_children_and_disposers_in_reverse_order() -> None:
    scope = LifecycleScope("root")
    log: list[str] = []
    child = scope.child("child")

    async def root_disposer() -> None:
        log.append("root")

    async def child_disposer() -> None:
        log.append("child")

    scope.add(root_disposer)
    child.add(child_disposer)
    await scope.close()
    await scope.close()
    assert log == ["child", "root"]
