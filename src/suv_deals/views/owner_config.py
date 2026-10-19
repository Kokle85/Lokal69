"""Owner approval path read models: tax rule sets and cost profiles (``api.owner_config_routes``).

The owner (``config:admin``, dashboard Settings) sees every stored rule-set version and every
repository draft under ``config/tax_rules/drafts`` that is not stored yet, and moves a version
through the lifecycle with explicit, audited owner acts (store a draft, submit for review, approve
with a named approver and a review record, activate, revoke). Nothing here approves or activates
anything by itself.

- ``content_sha256`` is the rule set's content hash: the sealed ``sha256`` once a version left
  draft, otherwise the hash computed from the draft's content (``sha256_sealed`` says which). The
  owner's mutations name it, so only the content the owner saw is ever submitted or approved.
- ``uncertain_items`` lists every statement and component of the rule set marked ``UNCERTAIN``;
  they are the open questions to resolve before an approval.
- ``allowed_actions`` are the owner acts the lifecycle permits from the current status (``store``
  for a draft file that is not stored yet); the server checks them again.
- Cost profiles: the stored versions and the CONFIGURED file (``cost_profiles/default_unapproved.yaml``,
  the profile the worker runtime values with), with its transport line. Amounts are decimal
  strings; an unknown amount is ``null`` with its status, never 0.
"""

from __future__ import annotations

from datetime import date
from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from suv_deals.domain.enums import CostCategory, CostLineStatus, TaxRuleStatus
from suv_deals.domain.money import CurrencyCode
from suv_deals.views.common import DecimalStr, Sha256Hex, UtcDatetime, ViewModel

#: Shown with every rule-set list: what the lifecycle means for production.
PRODUCTION_NOTE: Final = (
    "Valuations and alerts use only an ACTIVE, owner-approved rule set. Drafts, rule sets under review "
    "and approved-but-inactive versions are never used for them; the import-cost calculator shows them "
    "only as a labelled INDICATIVE PREVIEW."
)
COST_PROFILE_NOTE: Final = (
    "The worker runtime values with the configured cost profile file; approving its stored version "
    "records your approval of these assumptions (valuations that used the unapproved version are "
    "marked stale and recomputed). Unknown lines stay unknown, never zero."
)

RuleSetOrigin = Literal["stored", "draft_file"]
#: Owner acts on a rule set: ``store`` a draft file, or a lifecycle target.
TaxRuleAction = Literal["store", "under_review", "approved", "active", "revoked"]
CalculatorMode = Literal["active", "indicative_preview", "unavailable"]
CostProfileOrigin = Literal["stored", "configured_file"]


class RuleSourceView(ViewModel):
    """One legal source of a rule set (official URL, title, retrieval time, document hash)."""

    url: str = Field(max_length=2048)
    title: str = Field(max_length=300)
    retrieved_at: UtcDatetime
    sha256: Sha256Hex


class RuleComponentSummary(ViewModel):
    """One component as the reviewer lists it (no rates: those are in the rule-set file)."""

    id: str = Field(max_length=64)
    label: str = Field(max_length=200)
    category: CostCategory
    kind: str = Field(max_length=20)
    intermediate: bool = Field(description="A helper value: never counted in a total.")
    legal_reference: str | None = Field(max_length=500)
    uncertain: bool = Field(description="The label or legal reference is marked UNCERTAIN.")


class ReviewRecordView(ViewModel):
    reviewer: str = Field(max_length=200)
    reviewed_at: UtcDatetime
    content_sha256: Sha256Hex
    scope: str = Field(max_length=2000)
    professional_review: str | None = Field(max_length=500)
    evidence_refs: tuple[str, ...] = Field(max_length=50)
    notes: str | None = Field(max_length=2000)


class RuleApprovalView(ViewModel):
    """The recorded approval: the approver's name, the authenticated approving principal and the
    review record bound to the content hash."""

    approved_by: str = Field(max_length=200)
    approved_at: UtcDatetime
    approved_by_principal: UUID | None
    review_record: ReviewRecordView | None


class TaxRuleSetSummaryView(ViewModel):
    """One rule-set version: a stored one (``origin: stored``) or a repository draft file that
    this workspace does not hold yet (``origin: draft_file``)."""

    origin: RuleSetOrigin
    file_name: str | None = Field(
        max_length=160, description="Draft file name under config/tax_rules/drafts."
    )
    rule_set_id: str = Field(max_length=200)
    version: str = Field(max_length=80)
    label: str = Field(max_length=290)
    jurisdiction: str = Field(pattern=r"^[A-Z]{2}$")
    vehicle_categories: tuple[str, ...] = Field(max_length=20)
    status: TaxRuleStatus
    is_fixture: bool
    valid_from: date | None
    valid_to: date | None = Field(description="Exclusive end; null = open.")
    currency: CurrencyCode | None
    description: str | None = Field(max_length=2000)
    content_sha256: Sha256Hex
    sha256_sealed: bool = Field(description="The hash is sealed (the version left draft).")
    sources: tuple[RuleSourceView, ...] = Field(max_length=50)
    components: tuple[RuleComponentSummary, ...] = Field(max_length=100)
    uncertain_items: tuple[str, ...] = Field(max_length=200)
    approval: RuleApprovalView | None
    allowed_actions: tuple[TaxRuleAction, ...] = Field(max_length=5)
    production_selectable: bool = Field(
        description="ACTIVE, owner-approved and not a fixture: valuations may select it in its period."
    )
    created_at: UtcDatetime | None
    updated_at: UtcDatetime | None


class CalculatorSelectionView(ViewModel):
    """Which rule set the import-cost calculator uses today (its own selection)."""

    jurisdiction: str = Field(pattern=r"^[A-Z]{2}$")
    vehicle_category: str = Field(max_length=80)
    on_date: date
    mode: CalculatorMode
    rule_set: str | None = Field(max_length=290)
    status: TaxRuleStatus | None
    reason: str = Field(max_length=2000)


class TaxRuleSetsView(ViewModel):
    """``GET /api/settings/tax-rules`` (owner only)."""

    rule_sets: tuple[TaxRuleSetSummaryView, ...] = Field(max_length=200)
    file_notes: tuple[str, ...] = Field(
        max_length=50, description="Draft files that are unreadable or ignored (never stored from here)."
    )
    calculator: CalculatorSelectionView
    production_note: str = PRODUCTION_NOTE


class TaxRuleChangeResult(ViewModel):
    """Result of an owner act on a rule set (store a draft or a lifecycle transition)."""

    action: TaxRuleAction
    rule_set_id: str = Field(max_length=200)
    version: str = Field(max_length=80)
    label: str = Field(max_length=290)
    previous_status: TaxRuleStatus | None = Field(description="Null when a draft file was stored.")
    status: TaxRuleStatus
    content_sha256: Sha256Hex
    sha256_sealed: bool
    approved_by: str | None = Field(max_length=200)
    approved_at: UtcDatetime | None
    changed_at: UtcDatetime
    production_effect: str = Field(max_length=500)


class CostProfileLineView(ViewModel):
    """One cost-profile assumption (amounts are decimal strings; unknown is null, never 0)."""

    category: CostCategory
    label: str = Field(max_length=200)
    status: CostLineStatus
    currency: CurrencyCode
    low: DecimalStr | None
    base: DecimalStr | None
    high: DecimalStr | None
    note: str = Field(max_length=1000)
    scope: str | None = Field(max_length=300, description="Only for this case (e.g. origin_country=CH).")


class CostProfileSummaryView(ViewModel):
    """A cost-profile version: stored (``id`` set) or the configured file."""

    origin: CostProfileOrigin
    id: UUID | None = Field(description="The stored row (the configured file: its stored copy, if any).")
    profile_key: str = Field(max_length=80)
    version: int = Field(ge=1)
    basis: str = Field(max_length=2000)
    currency: CurrencyCode
    approval_status: Literal["unapproved", "approved", "not_stored"]
    approved_at: UtcDatetime | None
    approved_by_principal: UUID | None
    is_fixture: bool
    content_sha256: Sha256Hex = Field(description="Content hash without the approval record.")
    matches_stored: bool | None = Field(
        description=(
            "Configured file only: the stored copy of this version has the same content (null: not stored)."
        )
    )
    transport: CostProfileLineView | None = Field(description="The general (unscoped) transport line.")
    lines: tuple[CostProfileLineView, ...] = Field(max_length=200)
    can_approve: bool = Field(description="An owner approval is possible now (unapproved, not a fixture).")
    created_at: UtcDatetime | None


class CostProfilesView(ViewModel):
    """``GET /api/settings/cost-profiles`` (owner only)."""

    configured_file: str = Field(max_length=200)
    configured: CostProfileSummaryView | None
    configured_problem: str | None = Field(max_length=500)
    profiles: tuple[CostProfileSummaryView, ...] = Field(max_length=200)
    note: str = COST_PROFILE_NOTE


class CostProfileApproveResult(ViewModel):
    """Result of ``POST /api/settings/cost-profiles/approve``."""

    id: UUID
    profile_key: str = Field(max_length=80)
    version: int = Field(ge=1)
    approval_status: Literal["approved"] = "approved"
    approved_at: UtcDatetime
    content_sha256: Sha256Hex
    stored_now: bool = Field(description="The configured file's version was stored by this request first.")
    production_effect: str = Field(max_length=500)


__all__ = [
    "COST_PROFILE_NOTE",
    "PRODUCTION_NOTE",
    "CalculatorMode",
    "CalculatorSelectionView",
    "CostProfileApproveResult",
    "CostProfileLineView",
    "CostProfileOrigin",
    "CostProfileSummaryView",
    "CostProfilesView",
    "ReviewRecordView",
    "RuleApprovalView",
    "RuleComponentSummary",
    "RuleSetOrigin",
    "RuleSourceView",
    "TaxRuleAction",
    "TaxRuleChangeResult",
    "TaxRuleSetSummaryView",
    "TaxRuleSetsView",
]
