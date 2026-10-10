"""Findings shared by ``doctor`` and ``config validate`` (spec 26, 27, 30).

A `Finding` never carries a configured value: settings are reported by presence (``set`` /
``missing``), enumerated switches by their mode name, numbers only as the confirmed baseline
constants, and everything else as a fixed explanatory text.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal
from urllib.parse import urlsplit

import click

if TYPE_CHECKING:
    from suv_deals.settings import Settings

Status = Literal["ok", "info", "warn", "error", "skipped"]
_ORDER: Final[dict[str, int]] = {"error": 0, "warn": 1, "skipped": 2, "info": 3, "ok": 4}
_LABEL: Final[dict[str, str]] = {
    "ok": "OK",
    "info": "INFO",
    "warn": "WARN",
    "error": "ERROR",
    "skipped": "SKIP",
}
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True, slots=True)
class Finding:
    area: str
    name: str
    status: Status
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {k: str(v) for k, v in asdict(self).items()}


def has_errors(findings: Iterable[Finding]) -> bool:
    return any(f.status == "error" for f in findings)


def render(findings: Sequence[Finding], *, title: str) -> None:
    """Print findings. Details are built from fixed texts, presence words and mode names only, so
    they are printed as they are (the redaction heuristics would mangle "..._SECRET: missing")."""
    click.echo(title)
    width = max((len(f"{f.area}/{f.name}") for f in findings), default=10)
    for f in findings:
        label = _LABEL[f.status].ljust(5)
        click.echo(f"  {label} {f'{f.area}/{f.name}'.ljust(width)}  {f.detail}")
    counts = {status: sum(1 for f in findings if f.status == status) for status in _LABEL}
    click.echo(
        f"Summary: {counts['ok']} ok, {counts['info']} info, {counts['warn']} warnings, "
        f"{counts['skipped']} skipped, {counts['error']} errors"
    )


# --------------------------------------------------------------------------------------------
# Per-process configuration requirements (presence only)
# --------------------------------------------------------------------------------------------

Level = Literal["required", "recommended", "optional"]


@dataclass(frozen=True, slots=True)
class Requirement:
    setting: str  # Settings field name; the environment variable is its upper-case form
    level: Level
    why: str
    when: Callable[[Settings], bool] | None = None
    when_text: str = ""


def _network(s: Settings) -> bool:
    return s.source_network_enabled


def _oauth(s: Settings) -> bool:
    return s.mcp_auth_mode == "oauth"


def _supabase_snapshots(s: Settings) -> bool:
    return s.snapshot_storage == "supabase"


def _slack(s: Settings) -> bool:
    return s.notification_provider == "slack" or s.event_bridge_provider == "slack"


def _reply_signals(s: Settings) -> bool:
    """Seller-reply signals go to the private Slack channel (spec 37.6; the owner's route),
    independently of the candidate-discovery route (``slack.signal_send_blockers``)."""
    return s.allow_external_notifications and s.seller_reply_signal_provider == "slack"


def _slack_any(s: Settings) -> bool:
    return _slack(s) or _reply_signals(s)


def _events(s: Settings) -> bool:
    return s.mcp_events_enabled or s.event_bridge_provider == "mcp_events"


def _production(s: Settings) -> bool:
    return s.app_env == "production"


_SLACK_WHEN: Final = (
    "Slack is the candidate route or seller-reply signals use Slack "
    "(ALLOW_EXTERNAL_NOTIFICATIONS=true and SELLER_REPLY_SIGNAL_PROVIDER=slack)"
)
_DB: Final = Requirement("database_url", "required", "PostgreSQL/Supabase connection (server-side secret)")
_ROLE: Final = Requirement(
    "database_set_role", "recommended", "SET ROLE suv_backend on every connection (ADR 0001)"
)

PROCESS_REQUIREMENTS: Final[dict[str, tuple[Requirement, ...]]] = {
    "api": (
        _DB,
        _ROLE,
        Requirement("app_base_url", "required", "dashboard origin and links"),
        Requirement("mcp_cursor_signing_secret", "required", "signed pagination cursors (readiness)"),
        Requirement("supabase_url", "required", "dashboard sign-in: Supabase Auth issuer/JWKS"),
        Requirement(
            "mcp_public_url", "required", "MCP resource URL (.../mcp)", _oauth, "MCP_AUTH_MODE=oauth"
        ),
        Requirement(
            "mcp_oauth_issuer", "required", "OAuth authorization server", _oauth, "MCP_AUTH_MODE=oauth"
        ),
        Requirement(
            "mcp_oauth_jwks_url", "required", "OAuth token signing keys", _oauth, "MCP_AUTH_MODE=oauth"
        ),
        Requirement(
            "mcp_oauth_audience", "optional", "accepted audience (default: the resource URL)", _oauth
        ),
        Requirement("mcp_public_url", "recommended", "public MCP URL behind the HTTPS proxy", _production),
        Requirement(
            "supabase_secret_key",
            "required",
            "private evidence bucket",
            _supabase_snapshots,
            "SNAPSHOT_STORAGE=supabase",
        ),
    ),
    "worker": (
        _DB,
        _ROLE,
        Requirement(
            "crawl4ai_base_url", "required", "crawler endpoint", _network, "SOURCE_NETWORK_ENABLED=true"
        ),
        Requirement(
            "crawl4ai_api_token", "required", "crawler bearer token", _network, "SOURCE_NETWORK_ENABLED=true"
        ),
        Requirement(
            "supabase_url", "required", "evidence storage", _supabase_snapshots, "SNAPSHOT_STORAGE=supabase"
        ),
        Requirement(
            "supabase_secret_key",
            "required",
            "evidence storage",
            _supabase_snapshots,
            "SNAPSHOT_STORAGE=supabase",
        ),
        Requirement(
            "llm_api_key",
            "required",
            "LLM extraction fallback",
            lambda s: s.llm_extraction_enabled,
            "LLM_EXTRACTION_ENABLED=true",
        ),
        Requirement(
            "mobile_de_api_credentials",
            "required",
            "official mobile.de Search API",
            lambda s: s.mobile_de_api_enabled,
            "MOBILE_DE_API_ENABLED=true",
        ),
    ),
    "scheduler": (_DB, _ROLE),
    "reconciler": (_DB, _ROLE),
    "dispatcher": (
        _DB,
        _ROLE,
        Requirement("app_base_url", "required", "dashboard links in notifications"),
        Requirement(
            "slack_bot_token", "required", "Slack fallback / seller-reply signal", _slack_any, _SLACK_WHEN
        ),
        Requirement(
            "slack_signing_secret",
            "required",
            "Slack fallback / seller-reply signal",
            _slack_any,
            _SLACK_WHEN,
        ),
        Requirement(
            "slack_channel_id",
            "required",
            "the private Slack channel (seller-reply signal)",
            _slack_any,
            _SLACK_WHEN,
        ),
        Requirement(
            "slack_destination_approval_ref",
            "required",
            "approved destination (seller-reply signal)",
            _slack_any,
            _SLACK_WHEN,
        ),
        Requirement(
            "slack_team_id",
            "recommended",
            "workspace pinning of the Slack seller-reply route",
            _slack_any,
            _SLACK_WHEN,
        ),
        Requirement(
            "slack_bot_user_id",
            "recommended",
            "own-message loop protection (the bot's user id)",
            _slack_any,
            _SLACK_WHEN,
        ),
        Requirement(
            "mcp_event_subscription_secret_encryption_key",
            "required",
            "encrypting stored subscription secrets",
            _events,
            "native MCP Events selected",
        ),
    ),
    "crawler": (
        Requirement("crawl4ai_api_token", "required", "the crawler container's own bearer token"),
        Requirement("crawl4ai_image", "required", "pinned crawler image tag"),
        Requirement("crawl4ai_image_digest", "recommended", "immutable image digest (release step)"),
    ),
}
PROCESSES: Final = tuple(PROCESS_REQUIREMENTS)
_MISSING_STATUS: Final[dict[Level, Status]] = {"required": "error", "recommended": "warn", "optional": "info"}


def requirement_findings(settings: Settings, processes: Sequence[str]) -> list[Finding]:
    presence = settings.describe()
    findings: list[Finding] = []
    for process in processes:
        seen: set[str] = set()
        for req in PROCESS_REQUIREMENTS[process]:
            if req.when is not None and not req.when(settings):
                continue
            if req.setting in seen:
                continue
            seen.add(req.setting)
            env = req.setting.upper()
            present = presence.get(req.setting) == "set"
            condition = f" when {req.when_text}" if req.when_text else ""
            if present:
                status: Status = "ok"
                detail = f"{env}: set ({req.level}{condition}; {req.why})"
            else:
                status = _MISSING_STATUS[req.level]
                detail = f"{env}: missing ({req.level}{condition}; {req.why})"
            findings.append(Finding("config", process, status, detail))
    return findings


# --------------------------------------------------------------------------------------------
# Business baseline and configuration files
# --------------------------------------------------------------------------------------------

#: Environment name -> (Settings field, confirmed baseline, meaning). Spec 3 / 26.
_BASELINE: Final[tuple[tuple[str, str, Decimal, str], ...]] = (
    ("PRIMARY_MIN_PRICE_EUR", "primary_min_price_eur", Decimal(2500), "EUR 2,500 inclusive"),
    ("PRIMARY_MAX_PRICE_EUR", "primary_max_price_eur", Decimal(3000), "EUR 3,000 inclusive"),
    ("MAX_MILEAGE_KM_EXCLUSIVE", "max_mileage_km_exclusive", Decimal(200000), "strictly below 200,000 km"),
    ("MK_ASKING_BAND_MIN_EUR", "mk_asking_band_min_eur", Decimal(8000), "EUR 8,000"),
    ("MK_ASKING_BAND_MAX_EUR", "mk_asking_band_max_eur", Decimal(10000), "EUR 10,000"),
)


def baseline_findings(settings: Settings) -> list[Finding]:
    """The confirmed business rules cannot be changed through the environment (spec 26)."""
    findings: list[Finding] = []
    for env, field, expected, meaning in _BASELINE:
        value = getattr(settings, field)
        if value == expected:
            findings.append(Finding("business", env.lower(), "ok", f"{env} matches the baseline ({meaning})"))
        else:
            findings.append(
                Finding(
                    "business",
                    env.lower(),
                    "error",
                    f"{env} differs from the confirmed baseline ({meaning}); "
                    "the baseline acceptance test fails",
                )
            )
    if settings.manual_4000_profile_enabled:
        findings.append(
            Finding(
                "business",
                "manual_4000",
                "info",
                "MANUAL_4000_PROFILE_ENABLED=true: optional manual-review queue (never replaces the primary)",
            )
        )
    if not settings.contribution_threshold_approved:
        findings.append(
            Finding(
                "business",
                "contribution_threshold",
                "info",
                "minimum contribution EUR 1,500 remains a PROPOSAL (CONTRIBUTION_THRESHOLD_APPROVED=false)",
            )
        )
    return findings


def config_file_findings(config_dir: Path, settings: Settings) -> list[Finding]:
    """Business YAML, source registry, cost profile, taxonomy and tax rule files."""
    from suv_deals.errors import AppError

    findings: list[Finding] = []

    def problem_text(exc: AppError) -> str:
        from suv_deals.cli_commands._common import safe

        problems = exc.details.get("problems") if exc.details else None
        extra = f" ({'; '.join(str(p) for p in problems[:5])})" if isinstance(problems, list) else ""
        return safe(f"{exc.message}{extra}")

    if not config_dir.is_dir():
        return [Finding("files", "config_dir", "error", "CONFIG_DIR does not exist")]

    from suv_deals.domain.profiles import load_business_config

    try:
        business = load_business_config(config_dir)
    except AppError as exc:
        findings.append(Finding("files", "business_config", "error", problem_text(exc)))
    except (OSError, ValueError, KeyError) as exc:
        findings.append(Finding("files", "business_config", "error", f"cannot load: {type(exc).__name__}"))
    else:
        findings.append(
            Finding(
                "files",
                "business_config",
                "ok",
                f"defaults.yaml + {len(business.profiles)} profile(s) pass baseline validation",
            )
        )
        from suv_deals.domain.enums import ProfileKey

        manual = business.profiles.get(ProfileKey.MANUAL_4000)
        if manual is not None and manual.enabled != settings.manual_4000_profile_enabled:
            findings.append(
                Finding(
                    "files",
                    "manual_4000",
                    "warn",
                    "MANUAL_4000_PROFILE_ENABLED and profiles/manual_4000.yaml disagree; "
                    "the recorded configuration revision is authoritative",
                )
            )
        below = business.profiles.get(ProfileKey.BELOW_TARGET_WATCH)
        if below is not None and below.enabled != settings.below_target_watch_enabled:
            findings.append(
                Finding(
                    "files",
                    "below_target_watch",
                    "warn",
                    "BELOW_TARGET_WATCH_ENABLED and profiles/below_target_watch.yaml disagree",
                )
            )

    from suv_deals.adapters.registry import load_registry

    try:
        registry = load_registry(config_dir)
    except AppError as exc:
        findings.append(Finding("files", "sources", "error", problem_text(exc)))
    else:
        gates = registry.gates()
        active = sum(1 for g in gates if g.active)
        findings.append(
            Finding(
                "files",
                "sources",
                "ok",
                f"{len(gates)} source(s) valid; {active} active, {len(gates) - active} disabled or gated",
            )
        )

    cost_path = config_dir / "cost_profiles" / "default_unapproved.yaml"
    if cost_path.is_file():
        from suv_deals.domain.costs import load_cost_profile

        try:
            load_cost_profile(cost_path)
        except AppError as exc:
            findings.append(Finding("files", "cost_profile", "error", problem_text(exc)))
        else:
            findings.append(Finding("files", "cost_profile", "ok", f"{cost_path.name} is valid (unapproved)"))
    else:
        findings.append(
            Finding("files", "cost_profile", "warn", "no default cost profile; costs stay unknown")
        )

    taxonomy_path = config_dir / "vehicle_taxonomy.yaml"
    if taxonomy_path.is_file():
        from suv_deals.domain.taxonomy import load_taxonomy

        try:
            load_taxonomy(taxonomy_path)
        except AppError as exc:
            findings.append(Finding("files", "taxonomy", "error", problem_text(exc)))
        else:
            findings.append(Finding("files", "taxonomy", "ok", "vehicle_taxonomy.yaml is valid"))

    findings.extend(tax_rule_findings(config_dir / "tax_rules", settings))
    return findings


def tax_rule_findings(tax_dir: Path, settings: Settings) -> list[Finding]:
    from suv_deals.domain.enums import TaxRuleStatus
    from suv_deals.domain.tax_engine import load_rule_set_file
    from suv_deals.errors import AppError

    findings: list[Finding] = []
    production_ready = 0
    labels: list[str] = []
    for path in sorted(tax_dir.glob("*.json")) if tax_dir.is_dir() else []:
        try:
            rule_set = load_rule_set_file(path)
        except AppError as exc:
            findings.append(Finding("tax_rules", path.name, "error", exc.message))
            continue
        labels.append(rule_set.rule_set_id)
        usable = rule_set.status in (TaxRuleStatus.ACTIVE, TaxRuleStatus.APPROVED) and not rule_set.is_fixture
        production_ready += usable
        findings.append(
            Finding(
                "tax_rules",
                path.name,
                "ok",
                f"valid; status {rule_set.status.value}"
                + ("; fixture (never selectable)" if rule_set.is_fixture else "")
                + ("" if usable else "; not usable for production valuations"),
            )
        )
    if not production_ready:
        findings.append(
            Finding(
                "tax_rules",
                "activation",
                "info",
                "no approved/active rule set: import costs stay unknown and valuations incomplete (spec 16)",
            )
        )
    if settings.tax_rule_set_id and settings.tax_rule_set_id not in labels:
        findings.append(
            Finding(
                "tax_rules",
                "tax_rule_set_id",
                "warn",
                "TAX_RULE_SET_ID names no rule file in config/tax_rules",
            )
        )
    return findings


# --------------------------------------------------------------------------------------------
# Switches, routes and authentication (offline)
# --------------------------------------------------------------------------------------------


def switch_findings(settings: Settings) -> list[Finding]:
    findings = [
        Finding(
            "switches",
            "source_network",
            "info",
            f"SOURCE_NETWORK_ENABLED={str(settings.source_network_enabled).lower()}"
            + ("" if settings.source_network_enabled else " (every real source fetch is blocked)"),
        ),
        Finding(
            "switches",
            "external_notifications",
            "info",
            f"ALLOW_EXTERNAL_NOTIFICATIONS={str(settings.allow_external_notifications).lower()}"
            + ("" if settings.allow_external_notifications else " (no external delivery)"),
        ),
    ]
    if settings.fx_fetch_enabled:
        findings.append(
            Finding("switches", "fx_fetch", "info", "FX_FETCH_ENABLED=true (ECB reference rates)")
        )
    return findings


def notification_findings(settings: Settings) -> list[Finding]:
    from suv_deals.errors import AppError
    from suv_deals.integrations.event_bridge import select_activation_route

    modes = (
        f"NOTIFICATION_PROVIDER={settings.notification_provider}, "
        f"EVENT_BRIDGE_PROVIDER={settings.event_bridge_provider}, "
        f"EVENT_BRIDGE_ENABLED={str(settings.event_bridge_enabled).lower()}, "
        f"MCP_EVENTS_ENABLED={str(settings.mcp_events_enabled).lower()}"
    )
    findings = [Finding("notifications", "mode", "info", modes)]
    try:
        selection = select_activation_route(settings)
    except AppError as exc:
        findings.append(
            Finding("notifications", "route", "error", f"conflicting activation route: {exc.message}")
        )
        return findings
    detail = f"route {selection.route.value}, bridge_status {selection.bridge_status.value}"
    if selection.blockers:
        detail += "; blockers: " + "; ".join(selection.blockers[:6])
    findings.append(Finding("notifications", "route", "info", detail))
    findings.append(
        Finding(
            "notifications",
            "pull_mode",
            "ok",
            "pending reviews are always available through the dashboard and MCP tools (pull mode)",
        )
    )
    return findings


def seller_inquiry_findings(settings: Settings) -> list[Finding]:
    presence = settings.describe()
    findings = [
        Finding(
            "seller_inquiry",
            "mode",
            "info",
            f"SELLER_INQUIRY_MODE={settings.seller_inquiry_mode}, "
            f"SELLER_INQUIRY_KILL_SWITCH={str(settings.seller_inquiry_kill_switch).lower()}, "
            f"SELLER_REPLY_INGEST_MODE={settings.seller_reply_ingest_mode}, "
            f"SELLER_REPLY_SIGNAL_PROVIDER={settings.seller_reply_signal_provider}",
        )
    ]
    if settings.seller_inquiry_require_message_approval:
        findings.append(
            Finding(
                "seller_inquiry",
                "approval_gate",
                "warn",
                "SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL=true contradicts the standing authorization "
                "(spec 37.1)",
            )
        )
    if settings.seller_email_canary_send_enabled:
        findings.append(
            Finding(
                "seller_inquiry",
                "canary_send",
                "warn",
                "SELLER_EMAIL_CANARY_SEND_ENABLED=true: the owner's one-time activation canary step is"
                " armed (`suv-deals canary send` still needs every switch and"
                " --i-confirm-owner-controlled-address); set it back to false afterwards",
            )
        )
    if settings.seller_inquiry_mode == "automatic":
        for name in ("seller_email_provider", "seller_email_from", "seller_email_account_id"):
            if presence.get(name) != "set":
                findings.append(
                    Finding(
                        "seller_inquiry",
                        name,
                        "error",
                        f"{name.upper()}: missing (required when SELLER_INQUIRY_MODE=automatic)",
                    )
                )
    return findings


def _url_problem(name: str, value: str | None, settings: Settings, *, mcp_path: bool = False) -> str | None:
    if not value:
        return f"{name} is missing"
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return f"{name} must be an absolute http(s) URL"
    if parts.username is not None or parts.password is not None:
        return f"{name} must not contain credentials"
    if parts.query or parts.fragment:
        return f"{name} must not contain a query or fragment"
    if parts.scheme == "http" and not (
        settings.app_env in ("development", "test") and parts.hostname in LOOPBACK_HOSTS
    ):
        return f"{name} must use https (http only for a loopback development URL)"
    if mcp_path and parts.path.rstrip("/") != "/mcp":
        return f"{name} must end with /mcp"
    return None


def auth_findings(settings: Settings) -> list[Finding]:
    """Offline MCP/dashboard authentication metadata checks (no request is made)."""
    mode = settings.mcp_auth_mode
    findings = [Finding("auth", "mcp_mode", "info", f"MCP_AUTH_MODE={mode}")]

    def check(name: str, value: str | None, *, mcp_path: bool = False, required: bool = True) -> None:
        problem = _url_problem(name, value, settings, mcp_path=mcp_path)
        if problem is None:
            findings.append(Finding("auth", name.lower(), "ok", f"{name}: well-formed"))
        elif value or required:
            findings.append(Finding("auth", name.lower(), "error", problem))

    if mode == "oauth":
        check("MCP_PUBLIC_URL", settings.mcp_public_url, mcp_path=True)
        check("MCP_OAUTH_ISSUER", settings.mcp_oauth_issuer)
        check("MCP_OAUTH_JWKS_URL", settings.mcp_oauth_jwks_url)
        issuer = urlsplit((settings.mcp_oauth_issuer or "").strip()).hostname
        jwks = urlsplit((settings.mcp_oauth_jwks_url or "").strip()).hostname
        if issuer and jwks and issuer != jwks:
            findings.append(
                Finding("auth", "jwks_host", "warn", "the JWKS URL is on a different host than the issuer")
            )
        if not settings.mcp_oauth_audience:
            findings.append(
                Finding(
                    "auth", "mcp_oauth_audience", "info", "no audience set: tokens must name the resource URL"
                )
            )
    elif mode == "static_bearer":
        findings.append(
            Finding(
                "auth",
                "static_bearer",
                "info",
                "scoped static bearer credentials (not OAuth compliance; "
                "create with `credentials create-mcp`)",
            )
        )
        if settings.mcp_public_url:
            check("MCP_PUBLIC_URL", settings.mcp_public_url, mcp_path=True)
        elif settings.app_env == "production":
            findings.append(Finding("auth", "mcp_public_url", "error", "MCP_PUBLIC_URL is missing"))
    else:
        host = urlsplit((settings.mcp_public_url or settings.app_base_url or "").strip()).hostname
        if settings.app_env not in ("development", "test"):
            findings.append(
                Finding(
                    "auth",
                    "dev_local",
                    "error",
                    "MCP_AUTH_MODE=dev_local is allowed only in development/test",
                )
            )
        elif host not in LOOPBACK_HOSTS:
            findings.append(
                Finding("auth", "dev_local", "error", "MCP_AUTH_MODE=dev_local needs a loopback URL")
            )
        else:
            findings.append(Finding("auth", "dev_local", "ok", "development-only loopback credentials"))
    check("SUPABASE_URL", settings.supabase_url, required=False)
    if settings.app_env == "production":
        check("APP_BASE_URL", settings.app_base_url)
    return findings


def production_findings(settings: Settings) -> list[Finding]:
    if settings.app_env != "production":
        return []
    findings: list[Finding] = []
    if settings.mcp_auth_mode == "dev_local":
        findings.append(
            Finding("production", "mcp_auth_mode", "error", "dev_local authentication in production")
        )
    if not settings.app_base_url.startswith("https://"):
        findings.append(Finding("production", "app_base_url", "error", "APP_BASE_URL must use https"))
    if not settings.crawl4ai_image_digest:
        findings.append(
            Finding(
                "production", "crawl4ai_image_digest", "warn", "pin the tested crawler image digest (spec 4)"
            )
        )
    if settings.build_id in ("", "dev"):
        findings.append(Finding("production", "build_id", "warn", "BUILD_ID should record the release build"))
    return findings


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.area, _ORDER[f.status], f.name))
