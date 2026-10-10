"""Wave D2: which sources can be activated at all, and which can ever yield an inquiry recipient.

- OPS-03: a source whose adapter is a placeholder (``adapter_version: unimplemented``) is reported
  as "not activatable (no adapter)" by ``doctor`` and ``sources inspect`` -- no terms review,
  owner decision or switch can activate it (the runbook claims exactly this).
- F1: only acquisition sources whose adapter reports exact-ad seller-contact evidence
  (``SourceCapabilities.seller_contact_evidence``) can ever produce an inquiry recipient;
  ``doctor``, ``sources inspect`` and ``inquiries status`` name them, and with no ACTIVE producer
  in automatic mode the doctor warns that no listing can be inquired.

Synthetic settings only; ``--no-db``; nothing is contacted.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tests.cli.conftest import Cli

from suv_deals.adapters.registry import load_registry
from suv_deals.cli_commands.inquiries import _recipient_evidence_sources
from suv_deals.settings import Settings

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config"
BASE_ENV = {"CONFIG_DIR": str(CONFIG)}


def _source_findings(run_cli: Cli, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    result = run_cli("doctor", "--no-db", "--json", env=env)
    findings = json.loads(result.output)["findings"]
    return {f["name"]: f for f in findings if f["area"] == "sources"}


def test_registry_reports_activatability_and_seller_contact_evidence() -> None:
    gates = {g.source_key: g for g in load_registry(CONFIG).gates()}
    assert gates["pazar3_mk"].activatable is False
    assert gates["mobile_de_public"].activatable is False
    assert gates["example_dealer_template_de"].activatable is True
    assert gates["example_dealer_template_de"].seller_contact_evidence is True
    assert gates["pazar3_mk"].seller_contact_evidence is False
    as_dict = gates["example_dealer_template_de"].as_dict()
    assert as_dict["activatable"] is True and as_dict["seller_contact_evidence"] is True
    configured, active = load_registry(CONFIG).recipient_evidence_sources()
    assert "example_dealer_template_de" in configured and "pazar3_mk" not in configured
    assert active == []  # every source is disabled (owner decision)


def test_doctor_names_unimplemented_sources_not_activatable(run_cli: Cli) -> None:
    findings = _source_findings(run_cli, BASE_ENV)
    for key in ("pazar3_mk", "reklama5_mk", "subito_it", "autoscout24_de"):
        assert findings[key]["detail"].startswith("not activatable (no adapter"), findings[key]
    assert findings["example_dealer_template_de"]["detail"].startswith("not active:")


def test_doctor_reports_recipient_evidence_producers(run_cli: Cli) -> None:
    info = _source_findings(run_cli, BASE_ENV)["recipient_evidence"]
    assert info["status"] == "info"
    assert "active none" in info["detail"] and "example_dealer_template_de" in info["detail"]
    automatic = _source_findings(
        run_cli,
        {
            **BASE_ENV,
            "SELLER_INQUIRY_MODE": "automatic",
            "SELLER_EMAIL_PROVIDER": "outlook_local",
            "SELLER_EMAIL_ACCOUNT_ID": "synthetic-account",
            "SELLER_EMAIL_FROM": "owner-inquiries@example.invalid",
        },
    )["recipient_evidence"]
    assert automatic["status"] == "warn"
    assert "no listing can be inquired" in automatic["detail"]


def test_sources_inspect_shows_activatability_and_evidence(run_cli: Cli) -> None:
    placeholder = run_cli("sources", "inspect", "pazar3_mk", "--config-dir", str(CONFIG)).output
    assert "activatable       : no (no adapter" in placeholder
    assert "seller evidence   : no" in placeholder
    dealer = run_cli("sources", "inspect", "example_dealer_template_de", "--config-dir", str(CONFIG)).output
    assert "activatable       : yes" in dealer
    assert "seller evidence   : yes" in dealer


def test_inquiries_status_lists_active_recipient_evidence_sources() -> None:
    settings = Settings(_env_file=None, config_dir=CONFIG)  # type: ignore[call-arg]
    assert _recipient_evidence_sources(settings) == []
    broken = REPO / "tests" / "cli" / "__no_such_sources__"
    assert _recipient_evidence_sources(Settings(_env_file=None, config_dir=broken)) == []  # type: ignore[call-arg]
