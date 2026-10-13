"""``config validate`` (spec 26 baseline) and ``tax-rules validate`` (spec 16). Offline."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from tests.cli.conftest import Cli

from suv_deals.cli_commands._checks import tax_rule_findings
from suv_deals.settings import Settings

REPO = Path(__file__).resolve().parents[2]
SYNTHETIC_RULES = REPO / "tests" / "fixtures" / "tax" / "synthetic_rule_set.json"
EXAMPLE_RULES = REPO / "config" / "tax_rules" / "example_unapproved.json"


@pytest.fixture
def config_copy(tmp_path: Path) -> Path:
    target = tmp_path / "config"
    shutil.copytree(REPO / "config", target)
    return target


def test_config_validate_passes_on_the_repository_configuration(run_cli: Cli) -> None:
    result = run_cli("config", "validate")
    assert result.exit_code == 0, result.output
    assert "Summary:" in result.output
    assert "0 errors" in result.output


def test_config_validate_rejects_a_primary_max_of_eur_4000_from_the_environment(run_cli: Cli) -> None:
    result = run_cli("config", "validate", env={"PRIMARY_MAX_PRICE_EUR": "4000"})
    assert result.exit_code == 1
    assert "PRIMARY_MAX_PRICE_EUR differs from the confirmed baseline (EUR 3,000 inclusive)" in result.output


def test_config_validate_rejects_a_primary_max_of_eur_4000_in_the_yaml(
    run_cli: Cli, config_copy: Path
) -> None:
    path = config_copy / "profiles" / "primary.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["max_price_eur"] = "4000.00"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    result = run_cli("config", "validate", "--config-dir", str(config_copy))
    assert result.exit_code == 1
    assert "ERROR files/business_config" in result.output
    assert "EUR 2,500.00-3,000.00" in result.output


def test_config_validate_rejects_other_baseline_changes(run_cli: Cli) -> None:
    for env in ({"MAX_MILEAGE_KM_EXCLUSIVE": "200001"}, {"MK_ASKING_BAND_MIN_EUR": "7000"}):
        result = run_cli("config", "validate", env=env)
        assert result.exit_code == 1, env


def test_config_validate_json_output(run_cli: Cli) -> None:
    result = run_cli("config", "validate", "--json")
    data = json.loads(result.output)
    assert {"area", "name", "status", "detail"} <= set(data[0])


def test_config_validate_reports_invalid_settings_by_name_only(run_cli: Cli) -> None:
    result = run_cli("config", "validate", env={"DATABASE_POOL_MAX": "not-a-number-FAKE-77"})
    assert result.exit_code == 1
    assert "DATABASE_POOL_MAX" in result.output
    assert "FAKE-77" not in result.output


def test_config_validate_rejects_conflicting_activation_routes(run_cli: Cli) -> None:
    result = run_cli(
        "config", "validate", env={"MCP_EVENTS_ENABLED": "true", "NOTIFICATION_PROVIDER": "slack"}
    )
    assert result.exit_code == 1
    assert "conflicting activation route" in result.output


def test_config_validate_flags_a_message_approval_gate_and_missing_sender(run_cli: Cli) -> None:
    result = run_cli(
        "config",
        "validate",
        env={"SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL": "true", "SELLER_INQUIRY_MODE": "automatic"},
    )
    assert result.exit_code == 1
    assert "contradicts the standing authorization" in result.output
    assert "SELLER_EMAIL_PROVIDER: missing (required when SELLER_INQUIRY_MODE=automatic)" in result.output


def test_config_validate_reports_the_tax_rule_draft(run_cli: Cli, config_copy: Path) -> None:
    result = run_cli("config", "validate")
    assert result.exit_code == 0, result.output
    assert "tax_rules/drafts/mk_passenger_car_import_2026.10-draft.2.json" in result.output
    assert "unapproved draft: indicative preview only, never used for valuations" in result.output
    assert "no approved/active rule FILE in config/tax_rules" in result.output
    assert "rule sets STORED in the database" in result.output
    (config_copy / "tax_rules" / "drafts" / "broken.json").write_text(
        '{"rule_set_id": "x"}', encoding="utf-8"
    )
    broken = run_cli("config", "validate", "--config-dir", str(config_copy))
    assert broken.exit_code == 1
    assert "ERROR tax_rules/drafts/broken.json" in broken.output


def test_config_validate_rejects_an_invalid_source_file(run_cli: Cli, config_copy: Path) -> None:
    (config_copy / "sources" / "broken.yaml").write_text("source_key: Bad Key\n", encoding="utf-8")
    result = run_cli("config", "validate", "--config-dir", str(config_copy))
    assert result.exit_code == 1
    assert "ERROR files/sources" in result.output


def test_config_validate_production_checks(run_cli: Cli) -> None:
    result = run_cli(
        "config", "validate", env={"APP_ENV": "production", "APP_BASE_URL": "http://deals.example.invalid"}
    )
    assert result.exit_code == 1
    assert "APP_BASE_URL must use https" in result.output


# --------------------------------------------------------------------------------------------
# tax-rules validate
# --------------------------------------------------------------------------------------------


def test_tax_rules_validate_synthetic_fixture(run_cli: Cli) -> None:
    result = run_cli("tax-rules", "validate", str(SYNTHETIC_RULES))
    assert result.exit_code == 0, result.output
    assert "VALID    synthetic_rule_set.json" in result.output
    assert "SYNTHETIC fixture" in result.output
    assert "matches the recorded hash" in result.output
    assert "NOT selectable" in result.output


def test_tax_rules_validate_example_unapproved(run_cli: Cli) -> None:
    result = run_cli("tax-rules", "validate", str(EXAMPLE_RULES))
    assert result.exit_code == 0, result.output
    assert "status             : unapproved" in result.output
    assert "NOT selectable" in result.output


def test_tax_rules_validate_directory(run_cli: Cli) -> None:
    result = run_cli("tax-rules", "validate", str(REPO / "config" / "tax_rules"))
    assert result.exit_code == 0
    assert "example_unapproved.json" in result.output


def test_tax_rules_validate_drafts_directory(run_cli: Cli) -> None:
    # The North Macedonian draft ships unapproved: valid, unsealed, never selectable.
    result = run_cli("tax-rules", "validate", str(REPO / "config" / "tax_rules" / "drafts"))
    assert result.exit_code == 0, result.output
    assert "VALID    mk_passenger_car_import_2026.10-draft.2.json" in result.output
    assert "status             : draft" in result.output
    assert "not recorded (computed" in result.output
    assert "NOT selectable" in result.output
    assert "INVALID" not in result.output


def test_tax_rules_validate_rejects_tampered_content(run_cli: Cli, tmp_path: Path) -> None:
    data = json.loads(SYNTHETIC_RULES.read_text(encoding="utf-8"))
    data["jurisdiction"] = "YY"  # content changed after hashing
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    result = run_cli("tax-rules", "validate", str(path))
    assert result.exit_code == 1
    assert "INVALID  tampered.json" in result.output
    assert "sha256 does not match" in result.output


def test_tax_rules_validate_rejects_floats_and_duplicates(run_cli: Cli, tmp_path: Path) -> None:
    path = tmp_path / "dup.json"
    path.write_text('{"rule_set_id": "a", "rule_set_id": "b"}', encoding="utf-8")
    result = run_cli("tax-rules", "validate", str(path))
    assert result.exit_code == 1
    assert "duplicate JSON key" in result.output


def test_tax_rules_validate_empty_directory(run_cli: Cli, tmp_path: Path) -> None:
    result = run_cli("tax-rules", "validate", str(tmp_path))
    assert result.exit_code == 1
    assert "no *.json" in result.output


def test_tax_rule_set_id_of_the_shipped_draft_is_no_false_warning(config_copy: Path) -> None:
    """Regression: following docs/tax_rule_approval.md step 6 (TAX_RULE_SET_ID = the MK set)
    gave "TAX_RULE_SET_ID names no rule file", and the file-only check claimed no rule set was
    active even after the owner activated one in the database."""
    findings = tax_rule_findings(
        config_copy / "tax_rules", Settings.model_construct(tax_rule_set_id="mk-passenger-car-import")
    )
    assert not any(f.name == "tax_rule_set_id" for f in findings), findings
    activation = next(f for f in findings if f.name == "activation")
    assert "rule sets STORED in the database" in activation.detail
    unknown = tax_rule_findings(config_copy / "tax_rules", Settings.model_construct(tax_rule_set_id="nope"))
    assert any(f.name == "tax_rule_set_id" and f.status == "warn" for f in unknown)
