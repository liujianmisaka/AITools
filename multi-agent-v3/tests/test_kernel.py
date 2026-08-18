from dataclasses import dataclass

import pytest
from misaka_kernel import (
    EventDispatcher,
    Host,
    HostStatus,
    LifecycleScope,
    ModuleGraphError,
    ProfileDefinition,
    ProfileLoader,
    ServiceBinding,
    ServiceRegistry,
)
from misaka_kernel.lifecycle import AsyncDisposer
from misaka_kernel.registry import ProviderId
from misaka_kernel_contracts import (
    ModuleId,
    ModuleManifest,
    RuntimeEvent,
    ServiceKey,
    ServiceProvision,
    ServiceRequirement,
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

    async def attach(self, context: object) -> AsyncDisposer | None:
        if self.log is not None:
            self.log.append(f"attach:{self.module_id}")
        return None

    async def start(self, context: object) -> None:
        if self.log is not None:
            self.log.append(f"start:{self.module_id}")
        if self.fail_start:
            raise RuntimeError(self.module_id)


def _provision(key: str, *, name: str | None = None, multiple: bool = False) -> ServiceProvision:
    return ServiceProvision(ServiceKey(key), "1.0.0", name=name, multiple=multiple)


def test_service_registry_requires_explicit_named_provider() -> None:
    registry = ServiceRegistry()
    registry.register(
        ServiceBinding(ServiceKey("agent"), object(), "1.0.0", ProviderId("fake"))
    )
    assert registry.require(ServiceKey("agent")) is not None

    registry.register(
        ServiceBinding(ServiceKey("tool"), "a", "1.0.0", ProviderId("tool-a"), name="a")
    )
    with pytest.raises(Exception, match="named providers"):
        registry.require(ServiceKey("tool"))
    assert registry.require_named(ServiceKey("tool"), "a") == "a"


def test_profile_loader_selects_explicit_named_binding() -> None:
    class NamedModule(_Module):
        async def attach(self, context: object) -> AsyncDisposer | None:
            assert context is not None
            return None

        async def start(self, context: object) -> None:
            return None

    profile = ProfileDefinition(
        profile_id="named-profile",
        module_ids=(ModuleId("named"),),
        bindings={ServiceKey("tool"): "a"},
    )
    host = ProfileLoader({ModuleId("named"): lambda: NamedModule("named")}).create_host(profile)
    assert host.name == "named-profile"


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
    host.add_module(
        _Module("consumer", requires=(ServiceRequirement(ServiceKey("missing")),))
    )
    with pytest.raises(ModuleGraphError, match="requires"):
        await host.start()
    assert host.status is HostStatus.FAILED


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
