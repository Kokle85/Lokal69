"""The owner's approval path for tax rule sets and cost profiles (dashboard Settings; Supabase JWT).

Routes (``api.schemas.V11_DASHBOARD_ROUTES``, scope ``config:admin``: the workspace OWNER only;
reviewers and viewers get ``403``; system principals never reach a route). Same handler shape as
``api.inquiry_routes``: authenticate, check the scope, parse the closed model, run ONE short
workspace-scoped transaction and answer with the shared ``ResponseEnvelope``. Never an MCP tool.

- ``GET /api/settings/tax-rules``: every stored rule-set version (``valuation_repo.list_rule_sets``)
  and every repository DRAFT under ``config/tax_rules/drafts`` that this workspace does not hold
  yet: status, dates, currency, content hash (sealed, or computed from an unsealed draft),
  sources, components, approval record, the ``UNCERTAIN`` items and the lifecycle acts allowed
  now; plus the import-cost calculator's own selection for today.
- ``POST /api/settings/tax-rules/store``: store ONE draft file, named by its file NAME only (a name
  the listing shows; no path, no upload; a name with a separator or ``..``, or a symlink leaving
  the drafts directory, is refused), as status ``draft`` (``valuation_repo.store_rule_set``,
  audited ``tax_rules.store``). ``content_sha256`` must be the file's content hash. A version the
  workspace already holds is ``409`` (``details.reason`` ``already_stored`` or
  ``different_content``): content is versioned, never overwritten.
- ``POST /api/settings/tax-rules/transition``: ``under_review`` (seals the content hash),
  ``approved`` (named approver + review record bound to the content hash; the authenticated owner
  is recorded as the approving principal), ``active`` or ``revoked``
  (``valuation_repo.transition_tax_rule_set``: domain lifecycle + database trigger, audited). The
  owner's ``expected_status`` and ``content_sha256`` are compared under the row lock.
- ``GET /api/settings/cost-profiles`` and ``POST /api/settings/cost-profiles/approve``: the
  configured cost profile (the file the worker runtime values with, transport EUR 700) and the
  stored versions; approval through ``valuation_repo.approve_cost_profile`` (audited, content hash
  bound). When the configured file's version is not stored yet, it is stored unapproved first (as
  the worker runtime would on first use) in the same transaction.

Every mutation takes its ``idempotency_key`` in the body (an ``Idempotency-Key`` header, when sent,
must equal it) and runs in the transaction of its idempotency record: a retry with the same key
replays the original result, a failure consumes nothing. Nothing here approves, activates or
stores anything on its own; each act is one explicit owner request.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Request
from starlette.responses import Response

from suv_deals.api.deps import (
    AuthContext,
    Authenticated,
    api_state,
    body_model,
    check_idempotency_header,
    in_transaction,
    no_query,
    require_scope,
)
from suv_deals.api.errors import JSON_MEDIA_TYPE
from suv_deals.api.schemas import (
    DRAFT_FILE_NAME_PATTERN,
    V11_ROUTE_INDEX,
    ApiRoute,
    CostProfileApproveRequest,
    TaxRuleStoreRequest,
    TaxRuleTransitionRequest,
)
from suv_deals.clock import ensure_utc
from suv_deals.domain.actor import ActorContext
from suv_deals.domain.costs import CostAssumption, CostProfile, load_cost_profile
from suv_deals.domain.enums import CostCategory, TaxRuleStatus
from suv_deals.domain.import_estimate import DEFAULT_JURISDICTION, DEFAULT_VEHICLE_CATEGORY
from suv_deals.domain.tax_engine import (
    ALLOWED_TRANSITIONS,
    RuleSet,
    compute_rule_set_sha256,
    load_rule_set_file,
)
from suv_deals.errors import AppError, ErrorCode, NotFound, ValidationFailed, VersionConflict
from suv_deals.persistence import idempotency, valuation_repo
from suv_deals.persistence.database import Conn, db_now
from suv_deals.persistence.errors_map import TransientConflict
from suv_deals.persistence.queries.import_costs import (
    COST_PROFILE_FILE,
    DRAFT_RULES_DIR,
    select_calculator_rule_set,
)
from suv_deals.persistence.valuation_repo import StoredCostProfile, StoredRuleSet
from suv_deals.views.common import ResponseEnvelope, ViewModel, decimal_str, envelope
from suv_deals.views.owner_config import (
    CalculatorSelectionView,
    CostProfileApproveResult,
    CostProfileLineView,
    CostProfileSummaryView,
    CostProfilesView,
    ReviewRecordView,
    RuleApprovalView,
    RuleComponentSummary,
    RuleSourceView,
    TaxRuleAction,
    TaxRuleChangeResult,
    TaxRuleSetSummaryView,
    TaxRuleSetsView,
)

router = APIRouter()

STORE_OPERATION: Final = "owner_tax_rules_store"
TRANSITION_OPERATION: Final = "owner_tax_rules_transition"
APPROVE_PROFILE_OPERATION: Final = "owner_cost_profile_approve"
#: ``details.reason`` of a store refused because the workspace already holds that version.
ALREADY_STORED: Final = "already_stored"
DIFFERENT_CONTENT: Final = "different_content"
#: A review dated this far after the server time is refused (the browser clock may run ahead).
REVIEW_CLOCK_SKEW: Final = timedelta(minutes=5)
#: Lifecycle targets the dashboard offers (``api.schemas.TaxRuleTarget``), in lifecycle order.
_OWNER_TARGETS: Final[tuple[TaxRuleAction, ...]] = ("under_review", "approved", "active", "revoked")
_DRAFT_NAME_RE: Final = re.compile(DRAFT_FILE_NAME_PATTERN)
_SENTENCE_RE: Final = re.compile(r"(?<=\.)\s+(?=[A-Z\u0410-\u042f])")  # Latin or Cyrillic capital


def _route(key: str) -> ApiRoute:
    return V11_ROUTE_INDEX[key]


TAX_RULES: Final = _route("GET /api/settings/tax-rules")
STORE: Final = _route("POST /api/settings/tax-rules/store")
TRANSITION: Final = _route("POST /api/settings/tax-rules/transition")
COST_PROFILES: Final = _route("GET /api/settings/cost-profiles")
APPROVE_PROFILE: Final = _route("POST /api/settings/cost-profiles/approve")


# --------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------


def _respond(route: ApiRoute, body: ResponseEnvelope[Any]) -> Response:
    return Response(
        content=body.model_dump_json(), status_code=route.success_status, media_type=JSON_MEDIA_TYPE
    )


async def _now(conn: Conn) -> datetime:
    return ensure_utc(await db_now(conn))


def _replay_error(code: str) -> AppError:
    try:
        error_code = ErrorCode(code)
    except ValueError:
        error_code = ErrorCode.INTERNAL_ERROR
    return AppError(error_code, "The original request with this idempotency key failed")


async def _idempotent[R: ViewModel](
    conn: Conn,
    actor: ActorContext,
    operation: str,
    body: TaxRuleStoreRequest | TaxRuleTransitionRequest | CostProfileApproveRequest,
    *,
    model: type[R],
    work: Callable[[], Awaitable[R]],
) -> R:
    """Run ``work`` in the transaction of its idempotency record (same key + same request replays
    the stored result; a failure rolls the record back with everything else)."""
    request_hash = idempotency.request_hash_for(operation, body)
    started = await idempotency.begin(conn, actor, operation, body.idempotency_key, request_hash)
    if isinstance(started, idempotency.Replay):
        return model.model_validate(started.result)
    if isinstance(started, idempotency.ReplayError):
        raise _replay_error(started.error_code)
    if isinstance(started, idempotency.InProgress):
        raise TransientConflict.in_progress()
    result = await work()
    await idempotency.complete(conn, actor, operation, body.idempotency_key, result.model_dump(mode="json"))
    return result


async def _run[T](request: Request, auth: AuthContext, work: Callable[[Conn], Awaitable[T]]) -> T:
    return await in_transaction(api_state(request), auth.actor, work)


# --------------------------------------------------------------------------------------------
# Rule-set summaries
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DraftFile:
    """One readable DRAFT rule-set file of the repository (``config/tax_rules/drafts``)."""

    name: str
    rule_set: RuleSet


def drafts_directory(config_dir: Path) -> Path:
    return config_dir / DRAFT_RULES_DIR


def _inside(directory: Path, path: Path) -> bool:
    """A regular file directly in ``directory`` (a symlink leaving it does not count)."""
    resolved = path.resolve()
    return resolved.parent == directory.resolve() and resolved.is_file()


def scan_draft_files(directory: Path) -> tuple[list[DraftFile], list[str]]:
    """The readable ``status: draft``, non-fixture files (sorted by name) and a note per file that
    is unreadable or ignored. Files are only read here; nothing is stored."""
    files: list[DraftFile] = []
    notes: list[str] = []
    if not directory.is_dir():
        return files, notes
    for path in sorted(directory.glob("*.json")):
        if not _DRAFT_NAME_RE.fullmatch(path.name) or not _inside(directory, path):
            notes.append(f"{path.name[:160]}: ignored (not a plain draft file in the drafts directory)")
            continue
        try:
            rule_set = load_rule_set_file(path)
        except ValidationFailed as exc:
            notes.append(f"{path.name}: unreadable ({exc.message})"[:500])
            continue
        if rule_set.status != TaxRuleStatus.DRAFT or rule_set.is_fixture:
            notes.append(
                f"{path.name}: ignored (status {rule_set.status.value}"
                f"{', fixture' if rule_set.is_fixture else ''}; files under tax_rules/drafts are drafts only)"
            )
            continue
        files.append(DraftFile(name=path.name, rule_set=rule_set))
    return files, notes[:50]


def uncertain_items(rule_set: RuleSet) -> tuple[str, ...]:
    """Every statement of the description and every component marked ``UNCERTAIN``."""
    sentences = [s.strip() for s in _SENTENCE_RE.split(rule_set.description or "") if "UNCERTAIN" in s]
    components = [
        f"{comp.id}: " + " | ".join(t for t in (comp.label, comp.legal_reference or "") if "UNCERTAIN" in t)
        for comp in rule_set.components
        if "UNCERTAIN" in comp.label or "UNCERTAIN" in (comp.legal_reference or "")
    ]
    return tuple(item[:1200] for item in (*sentences, *components))


def allowed_actions(rule_set: RuleSet, *, stored: bool) -> tuple[TaxRuleAction, ...]:
    """The owner acts the lifecycle permits now (the server checks them again on every request)."""
    if not stored:
        return ("store",)
    permitted = ALLOWED_TRANSITIONS[rule_set.status]
    if rule_set.is_fixture:  # synthetic rule sets never enter review/approval
        permitted = permitted & {TaxRuleStatus.REVOKED}
    return tuple(t for t in _OWNER_TARGETS if TaxRuleStatus(t) in permitted)


def _approval(rule_set: RuleSet, stored: StoredRuleSet | None) -> RuleApprovalView | None:
    if rule_set.approved_by is None or rule_set.approved_at is None:
        return None
    record = rule_set.review_record
    return RuleApprovalView(
        approved_by=rule_set.approved_by,
        approved_at=rule_set.approved_at,
        approved_by_principal=None if stored is None else stored.approved_by_principal,
        review_record=None
        if record is None
        else ReviewRecordView(
            reviewer=record.reviewer,
            reviewed_at=record.reviewed_at,
            content_sha256=record.content_sha256,
            scope=record.scope,
            professional_review=record.professional_review,
            evidence_refs=record.evidence_refs,
            notes=record.notes,
        ),
    )


def rule_set_summary(
    rule_set: RuleSet, *, stored: StoredRuleSet | None, file_name: str | None = None
) -> TaxRuleSetSummaryView:
    return TaxRuleSetSummaryView(
        origin="stored" if stored is not None else "draft_file",
        file_name=file_name,
        rule_set_id=rule_set.rule_set_id,
        version=rule_set.version,
        label=rule_set.label(),
        jurisdiction=rule_set.jurisdiction,
        vehicle_categories=rule_set.vehicle_categories,
        status=rule_set.status,
        is_fixture=rule_set.is_fixture,
        valid_from=rule_set.valid_from,
        valid_to=rule_set.valid_to,
        currency=rule_set.currency,
        description=rule_set.description,
        content_sha256=rule_set.sha256 or compute_rule_set_sha256(rule_set),
        sha256_sealed=rule_set.sha256 is not None,
        sources=tuple(
            RuleSourceView(url=s.url, title=s.title, retrieved_at=s.retrieved_at, sha256=s.sha256)
            for s in rule_set.sources
        ),
        components=tuple(
            RuleComponentSummary(
                id=comp.id,
                label=comp.label,
                category=comp.category,
                kind=comp.kind,
                intermediate=comp.intermediate,
                legal_reference=comp.legal_reference,
                uncertain="UNCERTAIN" in comp.label or "UNCERTAIN" in (comp.legal_reference or ""),
            )
            for comp in rule_set.components
        ),
        uncertain_items=uncertain_items(rule_set),
        approval=_approval(rule_set, stored),
        allowed_actions=allowed_actions(rule_set, stored=stored is not None),
        production_selectable=rule_set.status == TaxRuleStatus.ACTIVE and not rule_set.is_fixture,
        created_at=None if stored is None else stored.created_at,
        updated_at=None if stored is None else stored.updated_at,
    )


def calculator_selection(
    stored: list[StoredRuleSet], files: list[DraftFile], on_date: date
) -> CalculatorSelectionView:
    """The import-cost calculator's own selection for ``on_date`` (production first, then the
    indicative preview); an ambiguous ACTIVE overlap selects nothing, with the reason."""
    base: dict[str, Any] = {
        "jurisdiction": DEFAULT_JURISDICTION,
        "vehicle_category": DEFAULT_VEHICLE_CATEGORY,
        "on_date": on_date,
    }
    try:
        selection = select_calculator_rule_set(
            stored, [f.rule_set for f in files], DEFAULT_JURISDICTION, DEFAULT_VEHICLE_CATEGORY, on_date
        )
    except ValidationFailed as exc:
        return CalculatorSelectionView(
            **base, mode="unavailable", rule_set=None, status=None, reason=exc.message[:2000]
        )
    chosen = selection.rule_set
    return CalculatorSelectionView(
        **base,
        mode="unavailable"
        if chosen is None
        else ("active" if selection.mode == "active" else "indicative_preview"),
        rule_set=None if chosen is None else chosen.label(),
        status=None if chosen is None else chosen.status,
        reason=selection.reason[:2000],
    )


async def tax_rule_sets_view(
    conn: Conn, actor: ActorContext, files: list[DraftFile], notes: list[str]
) -> TaxRuleSetsView:
    stored = await valuation_repo.list_rule_sets(conn, actor)
    by_label = {s.rule_set.label(): s for s in stored}
    summaries = [rule_set_summary(s.rule_set, stored=s) for s in stored]
    file_notes = list(notes)
    for draft in files:
        copy = by_label.get(draft.rule_set.label())
        if copy is None:
            summaries.append(rule_set_summary(draft.rule_set, stored=None, file_name=draft.name))
        elif compute_rule_set_sha256(copy.rule_set) != compute_rule_set_sha256(draft.rule_set):
            file_notes.append(
                f"{draft.name}: the stored {draft.rule_set.label()} ({copy.rule_set.status.value}) has "
                "DIFFERENT content; give the file a new version (content is never overwritten)"
            )
    on_date = (await _now(conn)).date()
    return TaxRuleSetsView(
        rule_sets=tuple(summaries[:200]),
        file_notes=tuple(note[:500] for note in file_notes[:50]),
        calculator=calculator_selection(stored, files, on_date),
    )


# --------------------------------------------------------------------------------------------
# Rule-set acts
# --------------------------------------------------------------------------------------------

_EFFECTS: Final[dict[str, str]] = {
    "store": (
        "Stored as a draft: NOT approved. Valuations and alerts never use it; the import-cost calculator "
        "may show it only as an indicative preview."
    ),
    "under_review": (
        "Submitted for review: the content hash is sealed and the content frozen. Still not used for "
        "valuations or alerts."
    ),
    "approved": (
        "Approved with the named approver and review record. Still not used for valuations or alerts "
        "until it is activated."
    ),
    "active": (
        "ACTIVE: valuations computed from now on (and the alerts built on them) use this rule set for its "
        "jurisdiction, vehicle categories and period."
    ),
    "revoked": (
        "Revoked: never selected again. Valuations that used it are marked stale and recomputed without it."
    ),
}


def change_result(
    action: TaxRuleAction, stored: StoredRuleSet, *, previous: TaxRuleStatus | None, at: datetime
) -> TaxRuleChangeResult:
    rule_set = stored.rule_set
    return TaxRuleChangeResult(
        action=action,
        rule_set_id=rule_set.rule_set_id,
        version=rule_set.version,
        label=rule_set.label(),
        previous_status=previous,
        status=rule_set.status,
        content_sha256=rule_set.sha256 or compute_rule_set_sha256(rule_set),
        sha256_sealed=rule_set.sha256 is not None,
        approved_by=rule_set.approved_by,
        approved_at=rule_set.approved_at,
        changed_at=at,
        production_effect=_EFFECTS[action],
    )


def resolve_draft_file(directory: Path, file_name: str) -> Path:
    """The draft file NAMED ``file_name`` directly in ``directory``: never a path, never a file
    outside it (``..``, separators and symlinks that leave the directory are refused)."""
    if not _DRAFT_NAME_RE.fullmatch(file_name) or ".." in file_name:
        raise ValidationFailed("file_name must be a draft file name", details={"fields": ["file_name"]})
    names = {p.name for p in directory.glob("*.json")} if directory.is_dir() else set()
    path = directory / file_name
    if file_name not in names or not _inside(directory, path):
        raise NotFound("No draft rule-set file with this name")
    return path


def load_draft_for_store(directory: Path, body: TaxRuleStoreRequest) -> RuleSet:
    """Read and check the named draft file (before the transaction: no file I/O inside it)."""
    rule_set = load_rule_set_file(resolve_draft_file(directory, body.file_name))
    if rule_set.status != TaxRuleStatus.DRAFT or rule_set.is_fixture:
        raise ValidationFailed(
            "Only status draft, non-fixture files are stored; approval and activation are owner "
            "transitions, never taken from a file",
            details={"fields": ["file_name"]},
        )
    if compute_rule_set_sha256(rule_set) != body.content_sha256:
        raise ValidationFailed(
            "content_sha256 is not the content hash of this draft file; reload and check it",
            details={"fields": ["content_sha256"]},
        )
    return rule_set


async def store_draft(conn: Conn, actor: ActorContext, rule_set: RuleSet) -> TaxRuleChangeResult:
    """Store a checked draft (``load_draft_for_store``) unless the workspace holds that version."""
    existing = await valuation_repo.list_rule_sets(conn, actor, jurisdiction=rule_set.jurisdiction)
    copy = next((s.rule_set for s in existing if s.rule_set.label() == rule_set.label()), None)
    if copy is not None:
        same = compute_rule_set_sha256(copy) == compute_rule_set_sha256(rule_set)
        raise VersionConflict(
            f"{rule_set.label()} is already stored ({copy.status.value})"
            + ("" if same else " with DIFFERENT content; give the file a new version"),
            reason=ALREADY_STORED if same else DIFFERENT_CONTENT,
            current_status=copy.status.value,
        )
    stored = await valuation_repo.store_rule_set(conn, actor, rule_set)
    return change_result("store", stored, previous=None, at=await _now(conn))


async def transition_rule_set(
    conn: Conn, actor: ActorContext, body: TaxRuleTransitionRequest
) -> TaxRuleChangeResult:
    """One owner lifecycle act against the status and content hash the owner saw."""
    now = await _now(conn)
    record = body.review_record
    if record is not None:
        if record.content_sha256 != body.content_sha256:
            raise ValidationFailed(
                "review_record.content_sha256 must be the content hash being approved",
                details={"fields": ["review_record.content_sha256"]},
            )
        if record.reviewed_at > now + REVIEW_CLOCK_SKEW:
            raise ValidationFailed(
                "review_record.reviewed_at is in the future",
                details={"fields": ["review_record.reviewed_at"]},
            )
    stored = await valuation_repo.transition_tax_rule_set(
        conn,
        actor,
        body.rule_set_id,
        body.version,
        TaxRuleStatus(body.target),
        approved_by=body.approved_by,
        review_record=None if record is None else record.to_domain(),
        expected_status=body.expected_status,
        expected_sha256=body.content_sha256,
    )
    return change_result(body.target, stored, previous=body.expected_status, at=now)


# --------------------------------------------------------------------------------------------
# Cost profiles
# --------------------------------------------------------------------------------------------


def profile_content_sha256(profile: CostProfile) -> str:
    """The profile's content hash without the approval record (what the owner approves)."""
    return profile.model_copy(
        update={"approval_status": "unapproved", "approved_by": None, "approved_at": None}
    ).sha256()


def configured_profile(config_dir: Path) -> tuple[CostProfile | None, str | None]:
    """The configured cost-profile file (the worker runtime's), or why it is unavailable."""
    path = config_dir / COST_PROFILE_FILE
    if not path.is_file():
        return None, f"no cost profile file at {COST_PROFILE_FILE.as_posix()}"
    try:
        return load_cost_profile(path), None
    except ValidationFailed as exc:
        return None, f"{COST_PROFILE_FILE.as_posix()} is invalid: {exc.message}"[:500]


def _scope_text(assumption: CostAssumption) -> str | None:
    scope = assumption.scope
    if scope is None:
        return None
    fields = scope.model_dump(exclude_none=True, exclude={"note"})
    return (", ".join(f"{k}={v}" for k, v in sorted(fields.items())) or "scoped")[:300]


def _line(assumption: CostAssumption) -> CostProfileLineView:
    def amount(value: Any) -> str | None:
        return None if value is None else decimal_str(value)

    return CostProfileLineView(
        category=assumption.category,
        label=assumption.label,
        status=assumption.status,
        currency=assumption.currency,
        low=amount(assumption.low),
        base=amount(assumption.base),
        high=amount(assumption.high),
        note=assumption.note,
        scope=_scope_text(assumption),
    )


def _transport(profile: CostProfile) -> CostProfileLineView | None:
    general = [a for a in profile.assumptions if a.category == CostCategory.TRANSPORT and a.scope is None]
    return _line(general[0]) if general else None


def profile_summary(
    profile: CostProfile, *, stored: StoredCostProfile | None, configured_file: bool
) -> CostProfileSummaryView:
    content = profile_content_sha256(profile)
    matches: bool | None = None
    if configured_file and stored is not None:
        matches = profile_content_sha256(stored.profile) == content
    source = stored.profile if stored is not None and not configured_file else profile
    approval = stored.profile.approval_status if stored is not None else "not_stored"
    unapproved = approval in ("unapproved", "not_stored")
    return CostProfileSummaryView(
        origin="configured_file" if configured_file else "stored",
        id=None if stored is None else stored.id,
        profile_key=source.profile_key,
        version=source.version,
        basis=source.basis,
        currency=source.currency,
        approval_status=approval,
        approved_at=None if stored is None else stored.profile.approved_at,
        approved_by_principal=None if stored is None else stored.approved_by_principal,
        is_fixture=source.is_fixture,
        content_sha256=content,
        matches_stored=matches,
        transport=_transport(source),
        lines=tuple(_line(a) for a in source.assumptions),
        can_approve=unapproved and not source.is_fixture and matches is not False,
        created_at=None if stored is None else stored.created_at,
    )


async def cost_profiles_view(
    conn: Conn, actor: ActorContext, configured: CostProfile | None, problem: str | None
) -> CostProfilesView:
    stored = await valuation_repo.list_cost_profiles(conn, actor)
    copy = None
    if configured is not None:
        copy = next(
            (
                s
                for s in stored
                if (s.profile.profile_key, s.profile.version) == (configured.profile_key, configured.version)
            ),
            None,
        )
    return CostProfilesView(
        configured_file=COST_PROFILE_FILE.as_posix(),
        configured=None
        if configured is None
        else profile_summary(configured, stored=copy, configured_file=True),
        configured_problem=problem,
        profiles=tuple(profile_summary(s.profile, stored=s, configured_file=False) for s in stored),
    )


async def approve_profile(
    conn: Conn, actor: ActorContext, body: CostProfileApproveRequest, configured: CostProfile | None
) -> CostProfileApproveResult:
    """Approve the named version (content hash bound); the configured file's version is stored
    first when this workspace does not hold it yet."""
    stored_now = False
    try:
        stored = await valuation_repo.find_cost_profile(conn, actor, body.profile_key, body.version)
    except NotFound:
        if configured is None or (configured.profile_key, configured.version) != (
            body.profile_key,
            body.version,
        ):
            raise
        if profile_content_sha256(configured) != body.content_sha256:
            raise ValidationFailed(
                "content_sha256 is not the content hash of the configured cost profile; reload and check it",
                details={"fields": ["content_sha256"]},
            ) from None
        stored = await valuation_repo.store_cost_profile(conn, actor, configured)
        stored_now = True
    if profile_content_sha256(stored.profile) != body.content_sha256:
        raise ValidationFailed(
            "content_sha256 is not the content hash of this cost-profile version; reload and check it",
            details={"fields": ["content_sha256"]},
        )
    approved = await valuation_repo.approve_cost_profile(conn, actor, stored.id)
    assert approved.profile.approved_at is not None
    return CostProfileApproveResult(
        id=approved.id,
        profile_key=approved.profile.profile_key,
        version=approved.profile.version,
        approved_at=approved.profile.approved_at,
        content_sha256=body.content_sha256,
        stored_now=stored_now,
        production_effect=(
            "Approved: valuations that used the unapproved version are marked stale and recomputed with the "
            "approved assumptions. Unknown lines stay unknown."
        ),
    )


# --------------------------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------------------------


@router.get("/api/settings/tax-rules")
async def get_tax_rules(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, TAX_RULES.scope)
    no_query(request)
    files, notes = scan_draft_files(drafts_directory(api_state(request).settings.config_dir))

    async def work(conn: Conn) -> tuple[TaxRuleSetsView, datetime]:
        view = await tax_rule_sets_view(conn, auth.actor, files, notes)
        return view, await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(TAX_RULES, envelope(view, request_id=auth.request_id, as_of=as_of))


@router.post("/api/settings/tax-rules/store")
async def post_tax_rules_store(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, STORE.scope)
    no_query(request)
    body = await body_model(request, TaxRuleStoreRequest)
    check_idempotency_header(request, body.idempotency_key)
    # The file is read before the transaction; a refusal is raised only for a NEW request, so a
    # retry of a stored request still replays its result.
    loaded: RuleSet | AppError
    try:
        loaded = load_draft_for_store(drafts_directory(api_state(request).settings.config_dir), body)
    except AppError as exc:
        loaded = exc

    async def store(conn: Conn) -> TaxRuleChangeResult:
        if isinstance(loaded, AppError):
            raise loaded
        return await store_draft(conn, auth.actor, loaded)

    async def work(conn: Conn) -> tuple[TaxRuleChangeResult, datetime]:
        result = await _idempotent(
            conn, auth.actor, STORE_OPERATION, body, model=TaxRuleChangeResult, work=lambda: store(conn)
        )
        return result, await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(STORE, envelope(view, request_id=auth.request_id, as_of=as_of))


@router.post("/api/settings/tax-rules/transition")
async def post_tax_rules_transition(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, TRANSITION.scope)
    no_query(request)
    body = await body_model(request, TaxRuleTransitionRequest)
    check_idempotency_header(request, body.idempotency_key)

    async def work(conn: Conn) -> tuple[TaxRuleChangeResult, datetime]:
        result = await _idempotent(
            conn,
            auth.actor,
            TRANSITION_OPERATION,
            body,
            model=TaxRuleChangeResult,
            work=lambda: transition_rule_set(conn, auth.actor, body),
        )
        return result, await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(TRANSITION, envelope(view, request_id=auth.request_id, as_of=as_of))


@router.get("/api/settings/cost-profiles")
async def get_cost_profiles(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, COST_PROFILES.scope)
    no_query(request)
    configured, problem = configured_profile(api_state(request).settings.config_dir)

    async def work(conn: Conn) -> tuple[CostProfilesView, datetime]:
        view = await cost_profiles_view(conn, auth.actor, configured, problem)
        return view, await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(COST_PROFILES, envelope(view, request_id=auth.request_id, as_of=as_of))


@router.post("/api/settings/cost-profiles/approve")
async def post_cost_profile_approve(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, APPROVE_PROFILE.scope)
    no_query(request)
    body = await body_model(request, CostProfileApproveRequest)
    check_idempotency_header(request, body.idempotency_key)
    configured, _problem = configured_profile(api_state(request).settings.config_dir)

    async def work(conn: Conn) -> tuple[CostProfileApproveResult, datetime]:
        result = await _idempotent(
            conn,
            auth.actor,
            APPROVE_PROFILE_OPERATION,
            body,
            model=CostProfileApproveResult,
            work=lambda: approve_profile(conn, auth.actor, body, configured),
        )
        return result, await _now(conn)

    view, as_of = await _run(request, auth, work)
    return _respond(APPROVE_PROFILE, envelope(view, request_id=auth.request_id, as_of=as_of))


__all__ = [
    "ALREADY_STORED",
    "APPROVE_PROFILE",
    "APPROVE_PROFILE_OPERATION",
    "COST_PROFILES",
    "DIFFERENT_CONTENT",
    "STORE",
    "STORE_OPERATION",
    "TAX_RULES",
    "TRANSITION",
    "TRANSITION_OPERATION",
    "DraftFile",
    "allowed_actions",
    "configured_profile",
    "cost_profiles_view",
    "profile_content_sha256",
    "resolve_draft_file",
    "router",
    "scan_draft_files",
    "tax_rule_sets_view",
    "uncertain_items",
]
