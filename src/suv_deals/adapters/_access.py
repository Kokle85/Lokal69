"""Access-state classification of fetched documents (spec sections 5, 8, 9, 25).

A successful HTTP status is not proof of usable content. This module separates:
- our own policy refusals (`policy_denied`),
- transport failures and 5xx (`transient_error`),
- 401/403, CAPTCHA/challenge pages, explicit automated-access denial, login walls and
  paywalls (`access_blocked`: stop the request path, never evade),
- 429 (`rate_limited`: back off, honour Retry-After),
- 404 (`not_found`) versus explicit removed-listing pages / 410 (`removed`),
- empty application shells, wrong page types and final-URL host mismatches
  (`unexpected_content`).

Markers are generic wording/attribute signals, not site-specific selectors. When the
page carries the structured content the caller expects (`content_present`), weak
signals such as an embedded reCAPTCHA contact-form widget do not mark the page as
blocked; strong denial wording in the page title still does. Without expected content the
order is: strong denial wording -> explicit removal wording -> weak widget markers ->
login wall -> paywall -> empty shell, so a removed-listing page with a footer reCAPTCHA or
a header login form is `removed`, not a source-wide block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from suv_deals.adapters._extract import PageView
from suv_deals.adapters.base import PageType, RawDocument
from suv_deals.domain.enums import AccessState
from suv_deals.netguard import UnsafeDestination, parse_safe_url

STRONG_CHALLENGE_MARKERS: tuple[str, ...] = (
    "cf-challenge",
    "cf_chl_",
    "challenge-platform",
    "are you a robot",
    "are you human",
    "verify you are human",
    "unusual traffic",
    "access denied",
    "zugriff verweigert",
    "sind sie ein roboter",
    "kein roboter sind",
    "bist du ein mensch",
    "accesso negato",
    "non sono un robot",
    "traffico insolito",
    "request blocked",
    "automated access",
    "automatisierte zugriffe",
    "bot detected",
)
WEAK_CHALLENGE_MARKERS: tuple[str, ...] = (
    "captcha",
    "g-recaptcha",
    "h-captcha",
    "hcaptcha",
    "cf-turnstile",
)
LOGIN_WORDS: tuple[str, ...] = (
    "anmelden",
    "einloggen",
    "melden sie sich an",
    "login",
    "log in",
    "sign in",
    "accedi",
    "effettua l'accesso",
    "connexion",
    "se connecter",
)
PAYWALL_MARKERS: tuple[str, ...] = (
    "paywall",
    "subscribe to continue",
    "subscription required",
    "jetzt abonnieren",
    "abo erforderlich",
    "nur für abonnenten",
    "abbonati per continuare",
    "riservato agli abbonati",
)
REMOVED_MARKERS: tuple[str, ...] = (
    "nicht mehr verfügbar",
    "nicht mehr verfuegbar",
    "inserat wurde deaktiviert",
    "inserat wurde gelöscht",
    "anzeige ist nicht mehr aktiv",
    "angebot ist nicht mehr aktiv",
    "fahrzeug wurde entfernt",
    "non è più disponibile",
    "non piu disponibile",
    "non più disponibile",
    "annuncio rimosso",
    "annuncio scaduto",
    "listing has been removed",
    "no longer available",
    "n'est plus disponible",
)
# Explicit "no results" wording. Bare counters such as "Merkliste (0 Fahrzeuge)" or
# "Vergleich: 0 Angebote" are NOT empty-result evidence: only result-count nouns
# ("0 Treffer", "0 risultati") or a count followed by "found" wording qualify, so a header
# widget on a page whose results failed to render is never reported as a complete empty search.
_EMPTY_RESULT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"keine (passenden )?fahrzeuge gefunden",
        r"keine (passenden )?angebote gefunden",
        r"keine treffer",
        r"keine (such)?ergebnisse",
        r"(?<![\d.,])0 (treffer|ergebnisse|suchergebnisse)\b",
        r"(?<![\d.,])0 (fahrzeuge|angebote) gefunden",
        r"nessun (risultato|veicolo|annuncio)",
        r"(?<![\d.,])0 risultati\b",
        r"(?<![\d.,])0 (veicoli|annunci) trovati",
        r"no (results|vehicles|matching vehicles) (found|available)",
        r"(?<![\d.,])0 (results|search results)\b",
        r"(?<![\d.,])0 vehicles found",
        r"aucun (résultat|véhicule)",
    )
)
EMPTY_SHELL_MAX_TEXT = 40


@dataclass(frozen=True, slots=True)
class AccessClassification:
    access_state: AccessState
    page_type: PageType
    evidence: str | None


def _first_marker(haystack: str, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if marker in haystack:
            return marker
    return None


def has_empty_result_marker(page: PageView) -> str | None:
    for pattern in _EMPTY_RESULT_PATTERNS:
        match = pattern.search(page.text_lower)
        if match:
            return match.group(0)
    return None


def has_removed_marker(page: PageView) -> str | None:
    return _first_marker(page.text_lower, REMOVED_MARKERS)


def _host_of(url: str | None) -> str | None:
    """Normalised (IDNA, lower-case) host of a URL; None for anything structurally unsafe."""
    if not url:
        return None
    try:
        return parse_safe_url(url.strip()).hostname
    except UnsafeDestination:
        return None


def _blocked_page_type(page: PageView | None, status: int | None) -> PageType:
    if page is None:
        return "login" if status == 401 else "unknown"
    haystack = f"{(page.title or '').lower()} {page.text_lower}"
    if _first_marker(haystack, STRONG_CHALLENGE_MARKERS) or _first_marker(
        page.raw_lower, WEAK_CHALLENGE_MARKERS
    ):
        return "challenge"
    if page.has_password_input or status == 401:
        return "login"
    if _first_marker(haystack, PAYWALL_MARKERS):
        return "paywall"
    return "unknown"


def classify_status_only(document: RawDocument) -> AccessClassification | None:
    """Classification from transport/status alone; None means "inspect the content"."""
    fetch = document.fetch
    status = fetch.http_status
    if fetch.access_state == AccessState.POLICY_DENIED:
        return AccessClassification(AccessState.POLICY_DENIED, "unknown", "request refused by URL policy")
    if status is None:
        if fetch.success and document.html is not None:
            return None
        state = fetch.access_state if fetch.access_state != AccessState.OK else AccessState.TRANSIENT_ERROR
        return AccessClassification(state, "unknown", fetch.error_code or "no HTTP response")
    if status in (401, 403, 407):
        return AccessClassification(AccessState.ACCESS_BLOCKED, "unknown", f"HTTP {status}")
    if status == 429:
        return AccessClassification(AccessState.RATE_LIMITED, "unknown", "HTTP 429")
    if status == 410:
        return AccessClassification(AccessState.REMOVED, "removed", "HTTP 410 Gone")
    if status == 404:
        return AccessClassification(AccessState.NOT_FOUND, "unknown", "HTTP 404")
    if status >= 500:
        return AccessClassification(AccessState.TRANSIENT_ERROR, "unknown", f"HTTP {status}")
    if not 200 <= status < 300:
        return AccessClassification(AccessState.UNEXPECTED_CONTENT, "unknown", f"HTTP {status}")
    if not fetch.success or fetch.access_state != AccessState.OK:
        # HTTP success with a failed crawl (spec section 8): the crawl client already classified
        # this response (anti-bot interstitial, stale cache hit, wait condition, empty or oversize
        # body). Its verdict is never upgraded to OK by looking at whatever HTML it kept.
        state = fetch.access_state if fetch.access_state != AccessState.OK else AccessState.UNEXPECTED_CONTENT
        evidence = f"HTTP {status}; crawl client reported {fetch.error_code or state.value}"
        return AccessClassification(state, "unknown", evidence)
    return None


def _content_denial(page: PageView, *, content_present: bool) -> AccessClassification | None:
    """Blocked/removed classification from page wording, or None.

    Without expected content the order is strong denial wording -> explicit removal wording ->
    weak widget markers -> login wall -> paywall. Removal outranks weak signals because
    removed-listing pages routinely keep a newsletter/contact reCAPTCHA widget or a header login
    form, and classifying them as blocked would pause the whole source route.
    """
    title_lower = (page.title or "").lower()
    strong = _first_marker(f"{title_lower} {page.text_lower}", STRONG_CHALLENGE_MARKERS) or _first_marker(
        page.raw_lower, ("cf-challenge", "cf_chl_", "challenge-platform")
    )
    if strong and (not content_present or strong in title_lower):
        return AccessClassification(AccessState.ACCESS_BLOCKED, "challenge", f"challenge marker '{strong}'")
    if content_present:
        return None
    removed = has_removed_marker(page)
    if removed:
        return AccessClassification(AccessState.REMOVED, "removed", f"removed-listing marker '{removed}'")
    weak = _first_marker(page.raw_lower, WEAK_CHALLENGE_MARKERS)
    if weak:
        return AccessClassification(AccessState.ACCESS_BLOCKED, "challenge", f"challenge marker '{weak}'")
    login = _first_marker(f"{title_lower} {page.text_lower}", LOGIN_WORDS)
    if page.has_password_input and login:
        return AccessClassification(AccessState.ACCESS_BLOCKED, "login", f"login wall ('{login}')")
    paywall = _first_marker(f"{title_lower} {page.text_lower}", PAYWALL_MARKERS)
    if paywall:
        return AccessClassification(AccessState.ACCESS_BLOCKED, "paywall", f"paywall marker '{paywall}'")
    return None


def classify_document(
    document: RawDocument,
    page: PageView | None,
    *,
    allowed_hosts: frozenset[str],
    content_present: bool,
) -> AccessClassification:
    """Full classification: status, final-URL host, then content markers."""
    fetch = document.fetch
    status = fetch.http_status
    status_class = classify_status_only(document)
    if status_class is not None:
        state = status_class.access_state
        if state == AccessState.ACCESS_BLOCKED:
            return AccessClassification(state, _blocked_page_type(page, status), status_class.evidence)
        if state == AccessState.TRANSIENT_ERROR and page is not None and status is not None:
            # Some challenge interstitials are served with 5xx; they are denials, not outages.
            marker = _first_marker(
                f"{(page.title or '').lower()} {page.text_lower}", STRONG_CHALLENGE_MARKERS
            )
            if marker:
                return AccessClassification(
                    AccessState.ACCESS_BLOCKED, "challenge", f"HTTP {status}; '{marker}'"
                )
        if state == AccessState.NOT_FOUND and page is not None and not content_present:
            marker = has_removed_marker(page)
            if marker:
                return AccessClassification(AccessState.REMOVED, "removed", f"HTTP 404; '{marker}'")
        if (
            state == AccessState.UNEXPECTED_CONTENT
            and page is not None
            and status is not None
            and 200 <= status < 300
        ):
            # The crawl client flagged a 2xx page (e.g. its wait condition failed). A challenge or an
            # explicit removal page explains why; refine to that state, never to OK. Weak widget
            # markers on a page that does carry the expected content do not turn it into a block.
            refined = _content_denial(page, content_present=content_present)
            if refined is not None:
                return AccessClassification(
                    refined.access_state, refined.page_type, f"{status_class.evidence}; {refined.evidence}"
                )
        return status_class

    final_host = _host_of(document.final_url or document.url)
    if allowed_hosts and final_host not in allowed_hosts:
        return AccessClassification(
            AccessState.UNEXPECTED_CONTENT, "unknown", f"final URL host {final_host!r} is not an allowed host"
        )
    content_type = (document.content_type or "").split(";", 1)[0].strip().lower()
    if content_type and "html" not in content_type and not content_type.endswith("xml"):
        return AccessClassification(
            AccessState.UNEXPECTED_CONTENT, "unknown", f"non-HTML content type {content_type[:60]!r}"
        )
    if page is None:
        return AccessClassification(
            AccessState.UNEXPECTED_CONTENT, "empty_shell", "empty or unparseable body"
        )

    denial = _content_denial(page, content_present=content_present)
    if denial is not None:
        return denial
    if (
        not content_present
        and len(page.text_lower) < EMPTY_SHELL_MAX_TEXT
        and page.json_ld.blocks == 0
        and not has_empty_result_marker(page)
    ):
        return AccessClassification(
            AccessState.UNEXPECTED_CONTENT,
            "empty_shell",
            f"only {len(page.text_lower)} characters of visible text and no structured content",
        )
    return AccessClassification(AccessState.OK, "unknown", None)
