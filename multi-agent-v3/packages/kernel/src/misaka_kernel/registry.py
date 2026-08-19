from __future__ import annotations

from dataclasses import dataclass
from typing import NewType, Protocol, cast

from misaka_kernel_contracts import ServiceKey, ServiceShape

from misaka_kernel.errors import ServiceResolutionError

ProviderId = NewType("ProviderId", str)


class ScopedServiceFactory(Protocol):
    def __call__(self, scope_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    key: ServiceKey
    value: object
    version: str
    provider_id: ProviderId
    shape: ServiceShape = ServiceShape.SINGLETON
    name: str | None = None


class ServiceRegistry:
    def __init__(self) -> None:
        self._singletons: dict[ServiceKey, ServiceBinding] = {}
        self._named: dict[ServiceKey, dict[str, ServiceBinding]] = {}
        self._scoped: dict[ServiceKey, ServiceBinding] = {}
        self._scoped_instances: dict[tuple[ServiceKey, str], object] = {}

    def register(self, binding: ServiceBinding) -> None:
        if binding.shape is ServiceShape.SINGLETON:
            if (
                binding.key in self._singletons
                or binding.key in self._named
                or binding.key in self._scoped
            ):
                raise ServiceResolutionError(
                    "service.duplicate",
                    f"service {binding.key} already has a binding",
                )
            self._singletons[binding.key] = binding
            return

        if binding.shape is ServiceShape.SCOPED:
            if (
                binding.key in self._singletons
                or binding.key in self._named
                or binding.key in self._scoped
            ):
                raise ServiceResolutionError(
                    "service.duplicate",
                    f"service {binding.key} already has a binding",
                )
            if not callable(binding.value):
                raise ServiceResolutionError(
                    "service.scoped_factory_required",
                    f"scoped service {binding.key} must be registered with a factory",
                )
            self._scoped[binding.key] = binding
            return

        if binding.name is None:
            raise ServiceResolutionError(
                "service.name_required",
                f"named service {binding.key} requires a provider name",
            )
        if binding.key in self._singletons or binding.key in self._scoped:
            raise ServiceResolutionError(
                "service.shape_conflict",
                f"service {binding.key} already has a non-named binding",
            )
        named = self._named.setdefault(binding.key, {})
        if binding.name in named:
            raise ServiceResolutionError(
                "service.named_duplicate",
                f"service {binding.key} has duplicate provider {binding.name}",
            )
        named[binding.name] = binding

    def unregister(self, binding: ServiceBinding) -> None:
        """Remove exactly the binding instance that was registered."""

        if binding.shape is ServiceShape.SINGLETON:
            if self._singletons.get(binding.key) is binding:
                del self._singletons[binding.key]
            return

        if binding.shape is ServiceShape.SCOPED:
            if self._scoped.get(binding.key) is binding:
                del self._scoped[binding.key]
                for cache_key in tuple(self._scoped_instances):
                    if cache_key[0] == binding.key:
                        del self._scoped_instances[cache_key]
            return

        if binding.name is None:
            return
        named = self._named.get(binding.key)
        if named is None:
            return
        if named.get(binding.name) is binding:
            del named[binding.name]
        if not named:
            del self._named[binding.key]

    def require(self, key: ServiceKey, *, scope_id: str | None = None) -> object:
        binding = self._singletons.get(key)
        if binding is not None:
            return binding.value
        scoped = self._scoped.get(key)
        if scoped is not None:
            if scope_id is None:
                raise ServiceResolutionError(
                    "service.scope_required",
                    f"scoped service {key} requires a scope",
                )
            cache_key = (key, scope_id)
            if cache_key not in self._scoped_instances:
                factory = cast(ScopedServiceFactory, scoped.value)
                self._scoped_instances[cache_key] = factory(scope_id)
            return self._scoped_instances[cache_key]
        named = self._named.get(key)
        if not named:
            raise ServiceResolutionError("service.missing", f"service {key} is not registered")
        raise ServiceResolutionError(
            "service.ambiguous",
            f"service {key} has named providers; choose one explicitly",
        )

    def resolve(
        self,
        key: ServiceKey,
        *,
        name: str | None = None,
        scope_id: str | None = None,
    ) -> object:
        if name is not None:
            return self.require_named(key, name)
        return self.require(key, scope_id=scope_id)

    def optional(self, key: ServiceKey, *, scope_id: str | None = None) -> object | None:
        if key in self._singletons:
            return self._singletons[key].value
        if key in self._scoped:
            return self.require(key, scope_id=scope_id)
        if key not in self._named:
            return None
        raise ServiceResolutionError(
            "service.ambiguous",
            f"service {key} has named providers; choose one explicitly",
        )

    def require_named(self, key: ServiceKey, name: str) -> object:
        binding = self._named.get(key, {}).get(name)
        if binding is None:
            raise ServiceResolutionError(
                "service.named_missing",
                f"service {key} provider {name} is not registered",
            )
        return binding.value

    def snapshot(self) -> tuple[ServiceBinding, ...]:
        values = list(self._singletons.values())
        for named in self._named.values():
            values.extend(named.values())
        values.extend(self._scoped.values())
        return tuple(sorted(values, key=lambda binding: (str(binding.key), binding.name or "")))

    def has_binding(
        self,
        key: ServiceKey,
        *,
        provider_id: ProviderId,
        version: str,
        shape: ServiceShape,
        name: str | None,
    ) -> bool:
        if shape is ServiceShape.SINGLETON:
            binding = self._singletons.get(key)
        elif shape is ServiceShape.SCOPED:
            binding = self._scoped.get(key)
        else:
            binding = self._named.get(key, {}).get(name or "")
        return binding is not None and (
            binding.provider_id == provider_id
            and binding.version == version
            and binding.shape is shape
            and binding.name == name
        )

    def clear_scope(self, scope_id: str) -> None:
        for cache_key in tuple(self._scoped_instances):
            if cache_key[1] == scope_id:
                del self._scoped_instances[cache_key]

    def clear(self) -> None:
        self._singletons.clear()
        self._named.clear()
        self._scoped.clear()
        self._scoped_instances.clear()
