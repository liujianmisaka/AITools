from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from misaka_kernel_contracts import JsonObject, ModuleId, ServiceKey

from misaka_kernel.configuration import validate_configuration
from misaka_kernel.errors import ModuleGraphError
from misaka_kernel.host import Host, Module

ModuleFactory = Callable[[], Module]


@dataclass(frozen=True, slots=True)
class CompositionSnapshot:
    """Immutable description of one resolved application composition."""

    profile_id: str
    profile_version: str
    module_ids: tuple[ModuleId, ...]
    bindings: tuple[tuple[str, str], ...]
    configuration_hash: str
    transport_ids: tuple[str, ...] = ()
    fact_owners: tuple[tuple[str, str], ...] = ()
    projection_sources: tuple[tuple[str, str], ...] = ()
    projection_watermark_owners: tuple[tuple[str, str], ...] = ()
    resource_owners: tuple[tuple[str, str], ...] = ()
    composition_hash: str = ""

    def __post_init__(self) -> None:
        if not self.profile_id.strip() or not self.profile_version.strip():
            raise ValueError("composition profile identity must not be empty")
        if not self.configuration_hash.strip() or not self.composition_hash.strip():
            raise ValueError("composition hashes must not be empty")
        _validate_named_pairs(self.fact_owners, "fact owners")
        _validate_named_pairs(self.projection_sources, "projection sources")
        _validate_named_pairs(self.projection_watermark_owners, "projection watermark owners")
        _validate_named_pairs(self.resource_owners, "resource owners")


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    profile_id: str
    module_ids: tuple[ModuleId, ...]
    profile_version: str = "1.0.0"
    bindings: dict[ServiceKey, str] = field(default_factory=dict)
    configurations: dict[ModuleId, JsonObject] = field(default_factory=dict)
    transport_ids: tuple[str, ...] = ()
    fact_owners: dict[str, str] = field(default_factory=dict)
    projection_sources: dict[str, str] = field(default_factory=dict)
    projection_watermark_owners: dict[str, str] = field(default_factory=dict)
    resource_owners: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("profile id must not be empty")
        if not self.profile_version.strip():
            raise ValueError("profile version must not be empty")
        if len(self.module_ids) != len(set(self.module_ids)):
            raise ValueError("profile module ids must be unique")
        if any(not value.strip() for value in self.transport_ids):
            raise ValueError("profile transport ids must not be empty")
        if len(self.transport_ids) != len(set(self.transport_ids)):
            raise ValueError("profile transport ids must be unique")
        _validate_mapping(self.fact_owners, "fact owners")
        _validate_mapping(self.projection_sources, "projection sources")
        _validate_mapping(self.projection_watermark_owners, "projection watermark owners")
        _validate_mapping(self.resource_owners, "resource owners")


class ProfileLoader:
    def __init__(self, factories: Mapping[ModuleId, ModuleFactory]) -> None:
        self._factories = dict(factories)

    def create_host(self, profile: ProfileDefinition) -> Host:
        missing = [
            module_id for module_id in profile.module_ids if module_id not in self._factories
        ]
        if missing:
            names = ", ".join(str(module_id) for module_id in missing)
            raise ModuleGraphError(
                "profile.module_missing",
                f"profile modules unavailable: {names}",
            )
        unknown_configurations = set(profile.configurations) - set(profile.module_ids)
        if unknown_configurations:
            names = ", ".join(sorted(str(module_id) for module_id in unknown_configurations))
            raise ModuleGraphError(
                "profile.configuration_module_missing",
                f"profile configures modules that are not selected: {names}",
            )
        modules: list[Module] = []
        for module_id in profile.module_ids:
            module = self._factories[module_id]()
            if module.manifest.module_id != module_id:
                raise ModuleGraphError(
                    "profile.module_identity_mismatch",
                    f"factory for {module_id} returned {module.manifest.module_id}",
                )
            validate_configuration(
                module_id,
                module.manifest.configuration_schema,
                profile.configurations.get(module_id, {}),
            )
            modules.append(module)
        host = Host(
            name=profile.profile_id,
            bindings=profile.bindings,
            configurations=profile.configurations,
            composition_snapshot=self.snapshot(profile),
        )
        for module in modules:
            host.add_module(module)
        return host

    def snapshot(self, profile: ProfileDefinition) -> CompositionSnapshot:
        configuration_payload = {
            str(module_id): configuration
            for module_id, configuration in sorted(
                profile.configurations.items(), key=lambda item: str(item[0])
            )
        }
        configuration_hash = _hash_payload(configuration_payload)
        composition_payload = {
            "profile_id": profile.profile_id,
            "profile_version": profile.profile_version,
            "module_ids": [str(module_id) for module_id in profile.module_ids],
            "bindings": sorted((str(key), value) for key, value in profile.bindings.items()),
            "configuration_hash": configuration_hash,
            "transport_ids": list(profile.transport_ids),
            "fact_owners": sorted(profile.fact_owners.items()),
            "projection_sources": sorted(profile.projection_sources.items()),
            "projection_watermark_owners": sorted(profile.projection_watermark_owners.items()),
            "resource_owners": sorted(profile.resource_owners.items()),
        }
        return CompositionSnapshot(
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            module_ids=profile.module_ids,
            bindings=tuple(sorted((str(key), value) for key, value in profile.bindings.items())),
            configuration_hash=configuration_hash,
            transport_ids=profile.transport_ids,
            fact_owners=tuple(sorted(profile.fact_owners.items())),
            projection_sources=tuple(sorted(profile.projection_sources.items())),
            projection_watermark_owners=tuple(sorted(profile.projection_watermark_owners.items())),
            resource_owners=tuple(sorted(profile.resource_owners.items())),
            composition_hash=_hash_payload(composition_payload),
        )


def _validate_mapping(values: Mapping[str, str], label: str) -> None:
    if any(not key.strip() or not value.strip() for key, value in values.items()):
        raise ValueError(f"profile {label} keys and values must not be empty")


def _validate_named_pairs(values: tuple[tuple[str, str], ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"composition {label} must be unique")
    _validate_mapping(dict(values), label)


def _hash_payload(payload: object) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
