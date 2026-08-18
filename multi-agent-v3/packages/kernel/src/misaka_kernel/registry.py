from __future__ import annotations

from dataclasses import dataclass
from typing import NewType

from misaka_kernel_contracts import ServiceKey

from misaka_kernel.errors import ServiceResolutionError

ProviderId = NewType("ProviderId", str)


@dataclass(frozen=True, slots=True)
class ServiceBinding:
    key: ServiceKey
    value: object
    version: str
    provider_id: ProviderId
    name: str | None = None


class ServiceRegistry:
    def __init__(self) -> None:
        self._singletons: dict[ServiceKey, ServiceBinding] = {}
        self._named: dict[ServiceKey, dict[str, ServiceBinding]] = {}

    def register(self, binding: ServiceBinding) -> None:
        if binding.name is None:
            if binding.key in self._singletons or binding.key in self._named:
                raise ServiceResolutionError(
                    "service.duplicate",
                    f"service {binding.key} already has a binding",
                )
            self._singletons[binding.key] = binding
            return

        if binding.key in self._singletons:
            raise ServiceResolutionError(
                "service.shape_conflict",
                f"service {binding.key} already has a singleton binding",
            )
        named = self._named.setdefault(binding.key, {})
        if binding.name in named:
            raise ServiceResolutionError(
                "service.named_duplicate",
                f"service {binding.key} has duplicate provider {binding.name}",
            )
        named[binding.name] = binding

    def require(self, key: ServiceKey) -> object:
        binding = self._singletons.get(key)
        if binding is not None:
            return binding.value
        named = self._named.get(key)
        if not named:
            raise ServiceResolutionError("service.missing", f"service {key} is not registered")
        raise ServiceResolutionError(
            "service.ambiguous",
            f"service {key} has named providers; choose one explicitly",
        )

    def resolve(self, key: ServiceKey, *, name: str | None = None) -> object:
        if name is not None:
            return self.require_named(key, name)
        return self.require(key)

    def optional(self, key: ServiceKey) -> object | None:
        if key in self._singletons:
            return self._singletons[key].value
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
        return tuple(sorted(values, key=lambda binding: (str(binding.key), binding.name or "")))

    def clear(self) -> None:
        self._singletons.clear()
        self._named.clear()
