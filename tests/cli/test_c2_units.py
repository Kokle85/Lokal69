"""Work package C2: the pure helpers behind the operator commands (no database, no network).

- ``canary.canary_send_blockers`` names every closed activation gate; ``canary_evidence`` reports
  the activation-evidence state of the configured sender binding only.
- ``doctor.secret_reference_findings``: ``SELLER_EMAIL_OAUTH_SECRET_REFERENCE`` must name the
  ACTIVE binding of ``SELLER_EMAIL_FROM`` (codes only, never the reference or an address).
- ``processes.reconcile_report_lines`` prints every ``ReconcileReport`` counter.
- ``BusinessConfig.claim_duration_seconds`` is documented as ignored and kept, because removing it
  would change the canonical hash of every stored configuration revision.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from suv_deals.api.schemas import MAX_REMOVABLE_SUPPRESSIONS
from suv_deals.cli_commands import canary as canary_cli
from suv_deals.cli_commands._common import CliAbort
from suv_deals.cli_commands.doctor import canary_finding, secret_reference_findings
from suv_deals.cli_commands.inquiries import MAX_REMOVABLE_SUPPRESSIONS as CLI_MAX_REMOVABLE
from suv_deals.cli_commands.processes import RECONCILE_REPORT_GROUPS, reconcile_report_lines
from suv_deals.domain.enums import EmailProviderKind
from suv_deals.domain.profiles import BusinessConfig, load_business_config
from suv_deals.persistence.canaries_repo import CanaryRecord
from suv_deals.persistence.config_repo import config_hash
from suv_deals.persistence.inquiries_repo import InquiryControls
from suv_deals.persistence.sender_bindings_repo import SenderBindingRecord, secret_reference_for
from suv_deals.settings import REPO_ROOT, Settings
from suv_deals.workers.reconciliation import ReconcileReport

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
FROM = "inquiries@synthetic-mail.example"


def _settings(**values: Any) -> Settings:
    return Settings(_env_file=None, **values)


def _binding(**values: Any) -> SenderBindingRecord:
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "provider": EmailProviderKind.GMAIL_API,
        "account_id": FROM,
        "from_address": FROM,
        "display_name": "Synthetic Sender",
        "alias_verified": True,
        "has_secret_envelope": True,
        "health": "healthy",
        "verified_at": NOW,
        "version": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(values)
    return SenderBindingRecord.model_validate(data)


def _controls(**values: Any) -> InquiryControls:
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "mode": "automatic",
        "kill_switch": False,
        "max_per_24h": 2,
        "max_per_15d": 5,
        "seller_cooldown": timedelta(days=7),
        "version": 4,
        "updated_at": NOW,
    }
    data.update(values)
    return InquiryControls.model_validate(data)


def _canary(binding: SenderBindingRecord, **values: Any) -> CanaryRecord:
    data: dict[str, Any] = {
        "id": uuid.uuid4(),
        "sender_binding_id": binding.id,
        "sender_binding_version": binding.version,
        "provider": binding.provider,
        "target_address_hash": "0" * 64,
        "rfc_message_id": "<canary-1@synthetic-mail.example>",
        "purpose": "Activation check (synthetic)",
        "state": "prepared",
        "outcome_evidence": {},
        "reply_evidence": {},
        "created_by": uuid.uuid4(),
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    data.update(values)
    return CanaryRecord.model_validate(data)


OPEN = {
    "seller_email_canary_send_enabled": True,
    "seller_inquiry_mode": "automatic",
}


# ---------------------------------------------------------------------------------- canary send gates


def test_every_gate_open_means_no_blocker() -> None:
    binding = _binding()
    assert (
        canary_cli.canary_send_blockers(
            _settings(**OPEN),
            confirmed=True,
            controls=_controls(),
            authorization_problems=[],
            sender=binding,
            identity_problems=[],
            canary=_canary(binding),
        )
        == []
    )


@pytest.mark.parametrize(
    ("settings", "controls", "auth", "sender_change", "identity", "canary_change", "confirmed", "code"),
    [
        ({**OPEN}, {}, [], {}, [], {}, False, "OWNER_CONTROLLED_ADDRESS_NOT_CONFIRMED"),
        ({"seller_inquiry_mode": "automatic"}, {}, [], {}, [], {}, True, "CANARY_SEND_SWITCH_OFF"),
        (
            {**OPEN, "seller_inquiry_mode": "paused"},
            {},
            [],
            {},
            [],
            {},
            True,
            "SELLER_INQUIRY_MODE_NOT_AUTOMATIC",
        ),
        (
            {**OPEN, "seller_inquiry_kill_switch": True},
            {},
            [],
            {},
            [],
            {},
            True,
            "SELLER_INQUIRY_KILL_SWITCH_ON",
        ),
        (
            {**OPEN, "seller_inquiry_require_message_approval": True},
            {},
            [],
            {},
            [],
            {},
            True,
            "MESSAGE_APPROVAL_SETTING_ON",
        ),
        ({**OPEN}, {"mode": "paused"}, [], {}, [], {}, True, "CONTROLS_MODE_NOT_AUTOMATIC"),
        (
            {**OPEN},
            {"kill_switch": True, "kill_switch_reason": "x", "kill_switch_set_at": NOW},
            [],
            {},
            [],
            {},
            True,
            "KILL_SWITCH_ACTIVE",
        ),
        (
            {**OPEN},
            {},
            ["standing_authorization_revoked"],
            {},
            [],
            {},
            True,
            "STANDING_AUTHORIZATION_REVOKED",
        ),
        ({**OPEN}, {}, [], {"verified_at": None}, [], {}, True, "SENDER_BINDING_UNVERIFIED"),
        ({**OPEN}, {}, [], {"health": "degraded"}, [], {}, True, "SENDER_BINDING_UNHEALTHY"),
        (
            {**OPEN},
            {},
            [],
            {},
            ["SENDER_BINDING_MISMATCH"],
            {},
            True,
            "SENDER_IDENTITY_SENDER_BINDING_MISMATCH",
        ),
        ({**OPEN}, {}, [], {}, [], {"state": "accepted"}, True, "CANARY_NOT_PREPARED"),
        ({**OPEN}, {}, [], {}, [], {"sender_binding_version": 2}, True, "CANARY_SENDER_VERSION_CHANGED"),
        ({**OPEN}, {}, [], {}, [], {"sender_binding_id": uuid.uuid4()}, True, "CANARY_SENDER_NOT_CONFIGURED"),
    ],
)
def test_each_closed_gate_is_named(
    settings: dict[str, Any],
    controls: dict[str, Any],
    auth: list[str],
    sender_change: dict[str, Any],
    identity: list[str],
    canary_change: dict[str, Any],
    confirmed: bool,
    code: str,
) -> None:
    binding = _binding()
    sender = binding.model_copy(update=sender_change)
    canary = _canary(binding, **canary_change)
    blockers = canary_cli.canary_send_blockers(
        _settings(**settings),
        confirmed=confirmed,
        controls=_controls(**controls),
        authorization_problems=auth,
        sender=sender,
        identity_problems=identity,
        canary=canary,
    )
    assert code in blockers


def test_a_canary_whose_desktop_mailbox_is_no_longer_active_is_blocked() -> None:
    """C2 review r2: an ``outlook_local`` canary bound to a desktop mailbox worker that was revoked
    (or no longer belongs to the configured sender) is never handed to a transport."""
    binding = _binding(provider=EmailProviderKind.OUTLOOK_LOCAL)
    canary = _canary(binding, mailbox_binding_id=uuid.uuid4())

    def blockers(mailbox_active: bool | None) -> list[str]:
        return canary_cli.canary_send_blockers(
            _settings(**OPEN),
            confirmed=True,
            controls=_controls(),
            authorization_problems=[],
            sender=binding,
            identity_problems=[],
            canary=canary,
            mailbox_active=mailbox_active,
        )

    assert blockers(False) == ["CANARY_MAILBOX_NOT_ACTIVE"]
    assert blockers(True) == []
    assert blockers(None) == []  # not checked (the command always checks a bound mailbox)


def test_default_settings_close_the_canary_send_and_nothing_is_registered() -> None:
    assert _settings().seller_email_canary_send_enabled is False  # safety default OFF
    blockers = canary_cli.canary_send_blockers(
        _settings(),
        confirmed=True,
        controls=None,
        authorization_problems=["standing_authorization_missing"],
        sender=None,
        identity_problems=["SENDER_BINDING_MISSING"],
        canary=None,
    )
    for code in (
        "CANARY_SEND_SWITCH_OFF",
        "SELLER_INQUIRY_MODE_NOT_AUTOMATIC",
        "CONTROLS_MISSING",
        "STANDING_AUTHORIZATION_MISSING",
        "SENDER_BINDING_MISSING",
        "CANARY_NOT_FOUND",
    ):
        assert code in blockers
    assert len(blockers) == len(set(blockers))
    assert canary_cli.CANARY_TRANSPORTS == {}  # no API provider can transmit a canary yet
    assert {"outlook_local"} == canary_cli.DESKTOP_CANARY_PROVIDERS  # F3: the desktop worker does


def test_target_address_comes_from_the_environment_or_a_prompt_only() -> None:
    assert canary_cli.read_target_address({canary_cli.CANARY_TARGET_ENV: "  a@b.example.invalid "}) == (
        "a@b.example.invalid"
    )
    with pytest.raises(CliAbort) as missing:
        canary_cli.read_target_address({})  # stdin is not a TTY under pytest
    assert canary_cli.CANARY_TARGET_ENV in missing.value.message
    assert "SUV_CANARY_TARGET_ADDRESS" not in Settings.model_fields  # never a settings/.env value
    assert "suv_canary_target_address" not in Settings.model_fields


# ---------------------------------------------------------------------------------- canary evidence


def test_canary_evidence_is_for_the_current_configured_binding() -> None:
    binding = _binding()
    assert canary_cli.canary_evidence([], None)[0] == "no_sender"
    assert canary_cli.canary_evidence([], binding)[0] == "none"
    other = _canary(_binding())
    assert canary_cli.canary_evidence([other], binding)[0] == "none"
    old = _canary(binding, sender_binding_version=2, state="reply_correlated")
    assert canary_cli.canary_evidence([old], binding)[0] == "stale"
    prepared = _canary(binding)
    assert canary_cli.canary_evidence([prepared, old], binding)[0] == "prepared"
    done = _canary(binding, state="reply_correlated")
    assert canary_cli.canary_evidence([prepared, done], binding)[0] == "complete"
    settings = _settings()
    assert canary_finding("", settings, [prepared, done], binding).status == "ok"
    assert canary_finding("", settings, [prepared], binding).status == "info"
    assert (
        canary_finding("", _settings(seller_inquiry_mode="automatic"), [prepared], binding).status == "warn"
    )
    # Migration 20261008000200 not applied yet (before the lead applies it): reported, never an error.
    unmigrated = canary_finding("", settings, None, binding)
    assert unmigrated.status == "info" and "20261008000200" in unmigrated.detail


# ---------------------------------------------------------------------------------- secret reference


def _api_settings(**values: Any) -> Settings:
    return _settings(
        seller_email_provider="gmail_api", seller_email_from=FROM, seller_email_account_id=FROM, **values
    )


def test_the_secret_reference_must_name_the_active_configured_binding() -> None:
    binding = _binding()
    ok = secret_reference_findings(
        "",
        _api_settings(seller_email_oauth_secret_reference=secret_reference_for(binding.id)),
        [binding],
        binding,
    )
    assert [f.status for f in ok] == ["ok"]
    missing = secret_reference_findings("", _api_settings(), [binding], binding)
    assert missing[0].status == "warn" and "SECRET_REFERENCE_MISSING" in missing[0].detail
    strict = secret_reference_findings("", _api_settings(seller_inquiry_mode="automatic"), [binding], binding)
    assert strict[0].status == "error"
    other = _binding(from_address="other@synthetic-mail.example", account_id="other@synthetic-mail.example")
    wrong = secret_reference_findings(
        "",
        _api_settings(seller_email_oauth_secret_reference=secret_reference_for(other.id)),
        [binding, other],
        binding,
    )
    detail = wrong[0].detail
    assert (
        "SECRET_REFERENCE_FROM_MISMATCH" in detail and "SECRET_REFERENCE_NOT_THE_CONFIGURED_BINDING" in detail
    )
    revoked = binding.model_copy(update={"revoked_at": NOW, "has_secret_envelope": False})
    gone = secret_reference_findings(
        "",
        _api_settings(seller_email_oauth_secret_reference=secret_reference_for(binding.id)),
        [revoked],
        None,
    )
    assert (
        "SECRET_REFERENCE_BINDING_REVOKED" in gone[0].detail
        and "SECRET_REFERENCE_NO_SEALED_GRANT" in gone[0].detail
    )
    unknown = secret_reference_findings(
        "",
        _api_settings(seller_email_oauth_secret_reference=secret_reference_for(uuid.uuid4())),
        [binding],
        binding,
    )
    assert "SECRET_REFERENCE_BINDING_UNKNOWN" in unknown[0].detail
    vault = secret_reference_findings(
        "", _api_settings(seller_email_oauth_secret_reference="vault:kv/suv/gmail"), [binding], binding
    )
    assert "SECRET_REFERENCE_UNSUPPORTED" in vault[0].detail
    token_like = secret_reference_findings(
        "",
        _api_settings(seller_email_oauth_secret_reference="ya29.synthetic-not-a-token"),
        [binding],
        binding,
    )
    assert "SECRET_REFERENCE_LOOKS_LIKE_CREDENTIAL" in token_like[0].detail
    for finding in (*ok, *missing, *wrong, *gone, *unknown, *vault, *token_like):
        assert FROM not in finding.detail and "ya29" not in finding.detail and "vault:" not in finding.detail
        assert str(binding.id) not in finding.detail


def test_outlook_local_needs_no_secret_reference() -> None:
    binding = _binding(provider=EmailProviderKind.OUTLOOK_LOCAL, has_secret_envelope=False)
    assert secret_reference_findings("", _settings(), [binding], binding) == []
    unused = secret_reference_findings(
        "",
        _settings(seller_email_oauth_secret_reference=secret_reference_for(binding.id)),
        [binding],
        binding,
    )
    assert unused[0].status == "warn" and "unused" in unused[0].detail


# ---------------------------------------------------------------------------------- reconcile report lines


def test_every_reconcile_counter_is_grouped_and_nothing_is_ever_dropped() -> None:
    report = ReconcileReport(workspace_id=uuid.uuid4(), dry_run=True, inquiry_retry_jobs=2).as_dict()
    for key in ("workspace_id", "errors", "dry_run"):
        report.pop(key)
    grouped = {name for _title, names in RECONCILE_REPORT_GROUPS for name in names}
    assert grouped == set(report)  # every current counter has its line
    lines = reconcile_report_lines(report)
    inquiries = next(line for line in lines if line.startswith("inquiries: "))
    assert "inquiry_retry_jobs=2" in inquiries
    future = reconcile_report_lines({**report, "brand_new_counter": 7})
    assert future[-1] == "other: brand_new_counter=7"


# ------------------------------------------------------------------------ resume bound, claim duration


def test_cli_resume_bound_equals_the_api_bound() -> None:
    assert CLI_MAX_REMOVABLE == MAX_REMOVABLE_SUPPRESSIONS


def test_claim_duration_seconds_is_ignored_and_kept_for_stored_revision_hashes() -> None:
    """``BusinessConfig.claim_duration_seconds`` is not read anywhere (the lease is
    ``REVIEW_CLAIM_DURATION_SECONDS``). It stays a field: every stored ``app.config_revisions``
    row contains it, and ``config_repo.record_config_revision`` compares the stored hash with the
    hash of the re-validated ``before`` configuration; dropping the field would change that hash
    and refuse every later ``config apply`` with ``VERSION_CONFLICT``."""
    config = load_business_config(REPO_ROOT / "config")
    stored = config.model_dump(mode="json")
    assert "claim_duration_seconds" in stored
    assert config_hash(BusinessConfig.model_validate(stored)) == config_hash(config)
    description = BusinessConfig.model_fields["claim_duration_seconds"].description or ""
    assert "REVIEW_CLAIM_DURATION_SECONDS" in description and "ignored" in description.lower()
