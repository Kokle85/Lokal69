"""Source configs, adapter registry and activation gate status (spec sections 5 and 32)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from tests.adapters.conftest import enabled_fixture_config, fixture_config

from suv_deals.adapters.base import SourceAdapter
from suv_deals.adapters.dealer_inventory import SchemaOrgDealerAdapter
from suv_deals.adapters.registry import (
    ADAPTERS,
    build_active_adapter,
    build_adapter,
    describe_gates,
    gate_status,
    load_registry,
    registry_problems,
)
from suv_deals.domain.enums import SourceMode, TechnicalStatus, TermsDecision, TermsStatus
from suv_deals.domain.sources import SourceConfig, activation_problems
from suv_deals.errors import SourcePaused, ValidationFailed

REPO = Path(__file__).resolve().parents[2]
SOURCES_DIR = REPO / "config" / "sources"
CONFIG_FILES = sorted(SOURCES_DIR.glob("*.yaml"))
EXPECTED_SOURCES = {
    "mobile_de_public",
    "autoscout24_de",
    "autoscout24_it",
    "autoscout24_ch",
    "subito_it",
    "automobile_it",
    "pazar3_mk",
    "reklama5_mk",
    "mobile_de_api",
    "example_dealer_template_de",
}


@pytest.mark.parametrize("path", CONFIG_FILES, ids=lambda p: p.name)
def test_every_source_file_validates_and_is_disabled(path: Path) -> None:
    config = SourceConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert path.stem == config.source_key
    assert config.enabled is False
    assert config.technical_status == TechnicalStatus.UNTESTED
    assert config.terms_decision == TermsDecision.PENDING
    assert config.terms_decision_actor is None
    assert (
        config.allowed_hosts == () and config.allowed_search_paths == () and config.allowed_detail_paths == ()
    )
    assert config.robots_policy == "obey" and config.technical_denial_policy == "stop_and_report"
    assert config.adapter in ADAPTERS
    assert activation_problems(config), "every candidate source must still be gated"


def test_expected_source_set() -> None:
    assert {p.stem for p in CONFIG_FILES} == EXPECTED_SOURCES


def test_mobile_de_public_terms_record() -> None:
    cfg = load_registry(REPO / "config").config("mobile_de_public")
    assert cfg.terms_status == TermsStatus.RESTRICTED
    assert cfg.terms_url == "https://www.mobile.de/service/agbPublic"
    assert cfg.terms_reviewed_at == datetime(2026, 10, 6, tzinfo=UTC)
    assert cfg.adapter_version == "unimplemented"
    assert cfg.notes is not None and "section 11" in cfg.notes


@pytest.mark.parametrize("key", ["autoscout24_de", "autoscout24_it"])
def test_autoscout_eu_terms_record(key: str) -> None:
    cfg = load_registry(REPO / "config").config(key)
    assert cfg.terms_status == TermsStatus.RESTRICTED
    assert cfg.terms_url == "https://www.autoscout24.com/company/agb/"
    assert cfg.notes is not None and "8.2-8.3" in cfg.notes and "verified" in cfg.notes


def test_autoscout_ch_is_reviewed_separately() -> None:
    cfg = load_registry(REPO / "config").config("autoscout24_ch")
    assert cfg.terms_status == TermsStatus.UNREVIEWED
    assert "terms not reviewed" in activation_problems(cfg)
    assert cfg.notes is not None and "Swiss" in cfg.notes


@pytest.mark.parametrize("key", ["subito_it", "automobile_it", "pazar3_mk", "reklama5_mk", "mobile_de_api"])
def test_unreviewed_sources(key: str) -> None:
    cfg = load_registry(REPO / "config").config(key)
    assert cfg.terms_status == TermsStatus.UNREVIEWED and cfg.terms_url is None


def test_roles_and_modes() -> None:
    registry = load_registry(REPO / "config")
    assert {c.source_key for c in registry.configs if c.role == "mk_comparable"} == {
        "pazar3_mk",
        "reklama5_mk",
    }
    assert all(registry.config(k).country == "MK" for k in ("pazar3_mk", "reklama5_mk"))
    assert registry.config("mobile_de_api").mode == SourceMode.OFFICIAL_API
    template = registry.config("example_dealer_template_de")
    assert template.adapter == "schemaorg_dealer" and template.adapter_version == "schemaorg_dealer@1.0.0"


def test_registry_builds_every_adapter_and_nothing_is_active() -> None:
    registry = load_registry(REPO / "config")
    assert {c.source_key for c in registry.configs} == EXPECTED_SOURCES
    for cfg in registry.configs:
        adapter = registry.adapter(cfg.source_key)
        assert isinstance(adapter, SourceAdapter)
        assert adapter.source_key == cfg.source_key
        with pytest.raises(SourcePaused):
            registry.active_adapter(cfg.source_key)
    assert registry.active_source_keys() == []
    gates = registry.gates()
    assert all(not g.active and g.problems for g in gates)
    assert all("source is disabled in configuration" in g.problems for g in gates)
    implemented = {g.source_key for g in gates if g.adapter_implemented}
    assert implemented == {"example_dealer_template_de"}
    json.dumps([g.as_dict() for g in gates])  # serialisable for API/MCP/status pages


def test_load_registry_accepts_the_sources_dir_itself() -> None:
    assert {c.source_key for c in load_registry(SOURCES_DIR).configs} == EXPECTED_SOURCES


def test_adapter_keys_match_classes() -> None:
    for key, cls in ADAPTERS.items():
        assert key == cls.ADAPTER_KEY


def test_registry_problems() -> None:
    cfg = fixture_config("fixture_dealer_de")
    assert registry_problems(cfg) == []
    assert "not registered" in registry_problems(cfg.model_copy(update={"adapter": "nope_adapter"}))[0]
    assert (
        "does not match"
        in registry_problems(cfg.model_copy(update={"adapter_version": "schemaorg_dealer@9"}))[0]
    )
    assert "not supported" in registry_problems(cfg.model_copy(update={"mode": SourceMode.OFFICIAL_API}))[0]
    with pytest.raises(ValidationFailed):
        build_adapter(cfg.model_copy(update={"adapter_version": "schemaorg_dealer@9"}))


def test_fixture_source_activation_requires_explicit_records() -> None:
    disabled = fixture_config("fixture_dealer_de")
    assert isinstance(build_adapter(disabled), SchemaOrgDealerAdapter)
    with pytest.raises(SourcePaused):
        build_active_adapter(disabled)
    pending = disabled.model_copy(update={"enabled": True})
    with pytest.raises(SourcePaused, match="terms"):
        build_active_adapter(pending)
    enabled = enabled_fixture_config("fixture_dealer_de")
    assert activation_problems(enabled) == []
    assert isinstance(build_active_adapter(enabled), SchemaOrgDealerAdapter)
    status = gate_status(enabled)
    assert status.active and status.gates_satisfied
    # Re-validating the in-memory record through the model also passes the activation guard.
    SourceConfig.model_validate(enabled.model_dump())
    # An owner acknowledgement of a restriction is an audit record; the gate still needs every other fact.
    acknowledged = enabled.model_copy(
        update={
            "terms_status": TermsStatus.RESTRICTED,
            "terms_decision": TermsDecision.PROCEED_ACKNOWLEDGED,
            "technical_status": TechnicalStatus.UNTESTED,
        }
    )
    assert "adapter has not passed fixture tests" in activation_problems(acknowledged)
    assert describe_gates([enabled, disabled])[1].active is False


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"a.yaml": {"source_key": "Bad Key"}}, "invalid source configuration"),
        (
            {
                "a.yaml": {
                    "source_key": "dup_source",
                    "display_name": "Synthetic A",
                    "country": "DE",
                    "role": "acquisition",
                    "mode": "public_html",
                    "adapter": "mobile_de_public",
                    "adapter_version": "unimplemented",
                },
                "b.yaml": {
                    "source_key": "dup_source",
                    "display_name": "Synthetic B",
                    "country": "DE",
                    "role": "acquisition",
                    "mode": "public_html",
                    "adapter": "mobile_de_public",
                    "adapter_version": "unimplemented",
                },
            },
            "duplicate",
        ),
        (
            {
                "a.yaml": {
                    "source_key": "odd_source",
                    "display_name": "Synthetic A",
                    "country": "DE",
                    "role": "acquisition",
                    "mode": "public_html",
                    "adapter": "not_registered",
                    "adapter_version": "unimplemented",
                }
            },
            "unregistered",
        ),
    ],
)
def test_load_registry_rejects_invalid_directories(
    tmp_path: Path, files: dict[str, object], message: str
) -> None:
    for name, data in files.items():
        (tmp_path / name).write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValidationFailed, match=message):
        load_registry(tmp_path)


def test_source_access_register_lists_every_source() -> None:
    register = (REPO / "docs" / "source_access_register.md").read_text(encoding="utf-8")
    for key in EXPECTED_SOURCES:
        assert f"`{key}`" in register, key
    lowered = register.lower()
    assert "not legal permission" in lowered
    assert "public accessibility is not permission" in lowered
