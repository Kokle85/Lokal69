"""Adapter registry and per-source activation gate status (spec sections 5, 8, 32).

`ADAPTERS` maps the `adapter` key used in `config/sources/*.yaml` to the adapter class.
Building an adapter is not activation: `build_adapter` works for disabled sources
(diagnostics, fixture tests), while `build_active_adapter` refuses unless the source
is enabled and every gate in `domain.sources.activation_problems` plus the registry's
own checks (known adapter, matching version, supported mode) are satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from pydantic import ValidationError

from suv_deals.adapters._placeholder import UNIMPLEMENTED, PlaceholderAdapter
from suv_deals.adapters.autoscout_public import (
    AutoScout24ChPublicAdapter,
    AutoScout24DePublicAdapter,
    AutoScout24ItPublicAdapter,
)
from suv_deals.adapters.base import SourceAdapter
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.adapters.it_marketplaces import AutomobileItPublicAdapter, SubitoPublicAdapter
from suv_deals.adapters.mk_comparables import Pazar3Adapter, Reklama5Adapter
from suv_deals.adapters.mobile_de_api import MobileDeSearchApiAdapter
from suv_deals.adapters.mobile_de_public import MobileDePublicAdapter
from suv_deals.domain.enums import TechnicalStatus, TermsDecision, TermsStatus
from suv_deals.domain.sources import SourceConfig, activation_problems, load_source_configs
from suv_deals.errors import NotFound, SourcePaused, ValidationFailed

AdapterClass = type[SchemaOrgDealerAdapter] | type[PlaceholderAdapter]

ADAPTERS: MappingProxyType[str, AdapterClass] = MappingProxyType(
    {
        SchemaOrgDealerAdapter.ADAPTER_KEY: SchemaOrgDealerAdapter,
        MobileDePublicAdapter.ADAPTER_KEY: MobileDePublicAdapter,
        AutoScout24DePublicAdapter.ADAPTER_KEY: AutoScout24DePublicAdapter,
        AutoScout24ItPublicAdapter.ADAPTER_KEY: AutoScout24ItPublicAdapter,
        AutoScout24ChPublicAdapter.ADAPTER_KEY: AutoScout24ChPublicAdapter,
        SubitoPublicAdapter.ADAPTER_KEY: SubitoPublicAdapter,
        AutomobileItPublicAdapter.ADAPTER_KEY: AutomobileItPublicAdapter,
        Pazar3Adapter.ADAPTER_KEY: Pazar3Adapter,
        Reklama5Adapter.ADAPTER_KEY: Reklama5Adapter,
        MobileDeSearchApiAdapter.ADAPTER_KEY: MobileDeSearchApiAdapter,
    }
)


def registry_problems(cfg: SourceConfig) -> list[str]:
    """Registry-level reasons a source cannot run (in addition to `activation_problems`)."""
    cls = ADAPTERS.get(cfg.adapter)
    if cls is None:
        return [f"adapter {cfg.adapter!r} is not registered"]
    problems: list[str] = []
    if cfg.adapter_version != cls.ADAPTER_VERSION:
        problems.append(
            f"configured adapter_version {cfg.adapter_version!r} does not match "
            f"code version {cls.ADAPTER_VERSION!r}"
        )
    if cfg.mode not in cls.SUPPORTED_MODES:
        problems.append(f"mode {cfg.mode.value} is not supported by adapter {cfg.adapter}")
    return problems


def build_adapter(cfg: SourceConfig) -> SourceAdapter:
    """Instantiate the configured adapter. Does NOT imply the source may run."""
    problems = registry_problems(cfg)
    if problems:
        raise ValidationFailed(f"source {cfg.source_key}: {'; '.join(problems)}")
    cls = ADAPTERS[cfg.adapter]
    adapter: SourceAdapter = cls(cfg)
    return adapter


def build_active_adapter(cfg: SourceConfig) -> SourceAdapter:
    """Adapter for network work; refuses unless the source is enabled and every gate passes."""
    if not cfg.enabled:
        raise SourcePaused(f"source {cfg.source_key} is not enabled")
    problems = activation_problems(cfg) + registry_problems(cfg)
    if problems:
        raise SourcePaused(f"source {cfg.source_key} is gated: {'; '.join(problems)}")
    return build_adapter(cfg)


@dataclass(frozen=True, slots=True)
class SourceGateStatus:
    """Separate terms and technical facts for one source (never collapsed into 'works')."""

    source_key: str
    display_name: str
    country: str
    role: str
    mode: str
    adapter: str
    adapter_version: str
    adapter_registered: bool
    adapter_implemented: bool
    enabled: bool
    technical_status: TechnicalStatus
    terms_status: TermsStatus
    terms_decision: TermsDecision
    terms_url: str | None
    terms_reviewed_at: datetime | None
    robots_policy: str
    problems: tuple[str, ...]

    @property
    def gates_satisfied(self) -> bool:
        return not self.problems

    @property
    def active(self) -> bool:
        return self.enabled and self.gates_satisfied

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_key": self.source_key,
            "display_name": self.display_name,
            "country": self.country,
            "role": self.role,
            "mode": self.mode,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "adapter_registered": self.adapter_registered,
            "adapter_implemented": self.adapter_implemented,
            "enabled": self.enabled,
            "technical_status": self.technical_status.value,
            "terms_status": self.terms_status.value,
            "terms_decision": self.terms_decision.value,
            "terms_url": self.terms_url,
            "terms_reviewed_at": self.terms_reviewed_at.isoformat() if self.terms_reviewed_at else None,
            "robots_policy": self.robots_policy,
            "gates_satisfied": self.gates_satisfied,
            "active": self.active,
            "problems": list(self.problems),
        }


def gate_status(cfg: SourceConfig) -> SourceGateStatus:
    problems = list(dict.fromkeys(activation_problems(cfg) + registry_problems(cfg)))
    if not cfg.enabled:
        problems.append("source is disabled in configuration")
    return SourceGateStatus(
        source_key=cfg.source_key,
        display_name=cfg.display_name,
        country=cfg.country,
        role=cfg.role,
        mode=cfg.mode.value,
        adapter=cfg.adapter,
        adapter_version=cfg.adapter_version,
        adapter_registered=cfg.adapter in ADAPTERS,
        adapter_implemented=cfg.adapter_version != UNIMPLEMENTED,
        enabled=cfg.enabled,
        technical_status=cfg.technical_status,
        terms_status=cfg.terms_status,
        terms_decision=cfg.terms_decision,
        terms_url=cfg.terms_url,
        terms_reviewed_at=cfg.terms_reviewed_at,
        robots_policy=cfg.robots_policy,
        problems=tuple(problems),
    )


def describe_gates(configs: list[SourceConfig] | tuple[SourceConfig, ...]) -> list[SourceGateStatus]:
    return [gate_status(cfg) for cfg in configs]


@dataclass(frozen=True, slots=True)
class SourceRegistry:
    configs: tuple[SourceConfig, ...]

    def config(self, source_key: str) -> SourceConfig:
        for cfg in self.configs:
            if cfg.source_key == source_key:
                return cfg
        raise NotFound(f"unknown source {source_key}")

    def adapter(self, source_key: str) -> SourceAdapter:
        return build_adapter(self.config(source_key))

    def active_adapter(self, source_key: str) -> SourceAdapter:
        return build_active_adapter(self.config(source_key))

    def gates(self) -> list[SourceGateStatus]:
        return describe_gates(self.configs)

    def active_source_keys(self) -> list[str]:
        return [g.source_key for g in self.gates() if g.active]


def load_registry(config_dir: Path) -> SourceRegistry:
    """Load `<config_dir>/sources/*.yaml` (or `config_dir/*.yaml` when given the sources dir)."""
    sources_dir = config_dir / "sources" if (config_dir / "sources").is_dir() else config_dir
    try:
        configs = load_source_configs(sources_dir)
    except ValidationError as exc:
        raise ValidationFailed(f"invalid source configuration: {exc.error_count()} error(s)") from None
    except yaml.YAMLError:
        raise ValidationFailed("invalid source configuration: a YAML file could not be parsed") from None
    except ValueError as exc:
        raise ValidationFailed(f"invalid source configuration: {exc}") from None
    unknown = [c.source_key for c in configs if c.adapter not in ADAPTERS]
    if unknown:
        raise ValidationFailed(f"sources reference unregistered adapters: {sorted(unknown)}")
    return SourceRegistry(configs=tuple(configs))
