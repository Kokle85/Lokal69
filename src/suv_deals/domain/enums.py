"""Shared enumerations. Values are persisted; never rename a value without a migration.

`UNKNOWN` members mean "not established". They are never equivalent to
zero, false or not-applicable (spec section 2).
"""

from __future__ import annotations

from enum import StrEnum


class Country(StrEnum):
    DE = "DE"
    IT = "IT"
    CH = "CH"
    MK = "MK"
    AT = "AT"
    FR = "FR"
    NL = "NL"
    BE = "BE"
    SI = "SI"
    HR = "HR"
    PL = "PL"
    CZ = "CZ"
    ES = "ES"


class Availability(StrEnum):
    AVAILABLE = "available"
    RESERVED = "reserved"
    REMOVED = "removed"  # never equated with sold
    SOLD_CLAIMED = "sold_claimed"
    UNKNOWN = "unknown"


class ClaimStatus(StrEnum):
    """Status of a seller/vehicle statement. Extraction confidence is separate."""

    VERIFIED = "verified"  # supported by owner-verified evidence (inspection, document)
    SELLER_CLAIMED = "seller_claimed"
    SELLER_DENIED = "seller_denied"  # seller positively states the opposite
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class Tristate(StrEnum):
    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class Precision(StrEnum):
    DAY = "day"
    MONTH = "month"
    YEAR = "year"
    UNKNOWN = "unknown"


class Fuel(StrEnum):
    DIESEL = "diesel"
    PETROL = "petrol"
    HYBRID_PETROL = "hybrid_petrol"
    HYBRID_DIESEL = "hybrid_diesel"
    PLUGIN_HYBRID = "plugin_hybrid"
    LPG = "lpg"
    CNG = "cng"
    ELECTRIC = "electric"
    OTHER = "other"
    UNKNOWN = "unknown"


class Gearbox(StrEnum):
    MANUAL = "manual"
    AUTOMATIC = "automatic"
    SEMI_AUTOMATIC = "semi_automatic"
    UNKNOWN = "unknown"


class Drive(StrEnum):
    FWD = "fwd"
    RWD = "rwd"
    AWD = "awd"  # permanent or automatic all-wheel drive
    FOUR_WD = "4wd"  # part-time / selectable 4x4
    UNKNOWN = "unknown"


class BodyType(StrEnum):
    SUV = "suv"
    OFFROAD = "offroad"
    CROSSOVER = "crossover"
    PICKUP = "pickup"
    ESTATE = "estate"
    SEDAN = "sedan"
    HATCHBACK = "hatchback"
    VAN = "van"
    OTHER = "other"
    UNKNOWN = "unknown"


class SteeringSide(StrEnum):
    LEFT = "left"
    RIGHT = "right"
    UNKNOWN = "unknown"


class PriceBasis(StrEnum):
    GROSS = "gross"
    NET = "net"
    UNKNOWN = "unknown"


class PriceType(StrEnum):
    FULL_VEHICLE_ASKING = "full_vehicle_asking"
    INSTALMENT = "instalment"
    LEASING = "leasing"
    DEPOSIT = "deposit"
    AUCTION_START = "auction_start"
    AUCTION_CURRENT_BID = "auction_current_bid"
    EXPORT_NET = "export_net"
    PARTS_OR_DAMAGED = "parts_or_damaged"
    PRICE_ON_REQUEST = "price_on_request"
    UNKNOWN = "unknown"


class VatTreatment(StrEnum):
    """Seller-stated VAT wording only; never an inferred entitlement."""

    VAT_SHOWN = "vat_shown"  # e.g. "MwSt. ausweisbar", "IVA esposta"
    MARGIN_SCHEME = "margin_scheme"  # e.g. "Differenzbesteuert §25a"
    PRIVATE_SALE = "private_sale"
    NOT_STATED = "not_stated"
    UNKNOWN = "unknown"


class SellerType(StrEnum):
    DEALER = "dealer"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class OdometerClaim(StrEnum):
    SELLER_REPORTED = "seller_reported"
    DOCUMENTED = "documented"  # service records/inspection referenced by seller
    VERIFIED = "verified"  # owner-verified evidence
    ESTIMATED = "estimated"  # seller gave a rough figure ("ca.", "approx")
    RANGE_ONLY = "range_only"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class Co2Cycle(StrEnum):
    NEDC = "nedc"
    NEDC_CORRELATED = "nedc_correlated"
    WLTP = "wltp"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """Extraction reliability. Says nothing about the truth of the seller claim."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExtractionMethod(StrEnum):
    JSON_LD = "json_ld"
    MICRODATA = "microdata"
    CSS = "css"
    XPATH = "xpath"
    REGEX = "regex"
    API_FIELD = "api_field"
    LLM_FALLBACK = "llm_fallback"
    MANUAL = "manual"
    DERIVED = "derived"


class AccessState(StrEnum):
    OK = "ok"
    ACCESS_BLOCKED = "access_blocked"  # 401/403/CAPTCHA/explicit automated denial/paywall/login
    RATE_LIMITED = "rate_limited"  # 429; back off, never evade
    NOT_FOUND = "not_found"
    REMOVED = "removed"  # explicit removed-listing page
    TRANSIENT_ERROR = "transient_error"  # timeouts, 5xx, connection failures
    UNEXPECTED_CONTENT = "unexpected_content"  # empty app shell, wrong page type, mismatched final URL
    POLICY_DENIED = "policy_denied"  # our own URL/robots policy refused the request


class Completeness(StrEnum):
    COMPLETE = "complete"  # provider cursor finished or cutoff reached under a stable sort
    BUDGET_LIMITED = "budget_limited"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"


class CoverageMode(StrEnum):
    WATERMARK = "watermark"
    ROLLING_PAGES = "rolling_pages"


class TechnicalStatus(StrEnum):
    UNTESTED = "untested"
    FIXTURE_TESTED = "fixture_tested"
    LIVE_SMOKE_PASSED = "live_smoke_passed"
    DEGRADED = "degraded"
    PARSER_UNHEALTHY = "parser_unhealthy"
    ACCESS_BLOCKED = "access_blocked"


class TermsStatus(StrEnum):
    UNREVIEWED = "unreviewed"
    PERMITTED = "permitted"  # explicit permission/agreement on file
    NO_RESTRICTION_FOUND = "no_restriction_found"  # not the same as a licence
    RESTRICTED = "restricted"


class TermsDecision(StrEnum):
    PENDING = "pending"
    # Owner acknowledged a restriction: an audit record only, not permission.
    PROCEED_ACKNOWLEDGED = "proceed_acknowledged"
    PROCEED_PERMITTED = "proceed_permitted"  # permission/agreement on file
    DO_NOT_USE = "do_not_use"


class SourceMode(StrEnum):
    PUBLIC_HTML = "public_html"
    OFFICIAL_API = "official_api"
    FIXTURE = "fixture"


class EligibilityState(StrEnum):
    ELIGIBLE_PRIMARY = "eligible_primary"
    ELIGIBLE_MANUAL_PROFILE = "eligible_manual_profile"
    NEEDS_FACTS = "needs_facts"
    REJECTED = "rejected"


class ValuationState(StrEnum):
    NOT_STARTED = "not_started"
    INCOMPLETE = "incomplete"
    ESTIMATED = "estimated"
    QUOTE_SUPPORTED = "quote_supported"
    STALE = "stale"
    INVALID = "invalid"


class ReviewState(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    NEEDS_INFORMATION = "needs_information"
    WATCH = "watch"
    SHORTLISTED = "shortlisted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ReviewOutcome(StrEnum):
    NEEDS_INFORMATION = "needs_information"
    WATCH = "watch"
    SHORTLISTED = "shortlisted"
    REJECTED = "rejected"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    BLOCKED = "blocked"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class JobType(StrEnum):
    DISCOVERY = "discovery"
    DETAIL = "detail"
    RECHECK = "recheck"
    VALUATION = "valuation"
    COMPARABLES = "comparables"
    STALE_SWEEP = "stale_sweep"
    REPROCESS = "reprocess"
    # Spec v1.1 section 37 runtime (ops.jobs ``jobs_type_ck``, migration 20261007000100).
    #: Evaluate inquiry readiness and reserve (quota debit + binding) under the guards.
    SELLER_INQUIRY_PLAN = "seller_inquiry_plan"
    #: Server-side provider send, or publication of an ``outlook_local`` send intent.
    SELLER_INQUIRY_SEND = "seller_inquiry_send"
    #: Reconcile an uncertain send with positive evidence only (never a blind resend).
    SELLER_INQUIRY_RECONCILE = "seller_inquiry_reconcile"
    #: Process a stored reply: claims, evidence, availability events, valuation invalidation.
    SELLER_REPLY_PROCESS = "seller_reply_process"


class OutboxState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY_WAIT = "retry_wait"
    DELIVERED = "delivered"
    UNCERTAIN = "uncertain"
    BLOCKED = "blocked"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class EvidenceKind(StrEnum):
    """MK market evidence (spec section 15)."""

    ASKING_PRICE = "asking_price"
    SELLER_REPORTED_SALE = "seller_reported_sale"
    VERIFIED_SALE = "verified_sale"
    OWNER_ESTIMATE = "owner_estimate"


class CostLineStatus(StrEnum):
    QUOTED = "quoted"
    ESTIMATED = "estimated"
    ACTUAL = "actual"
    NOT_APPLICABLE = "not_applicable"  # requires a reason
    UNKNOWN = "unknown"


class CostCategory(StrEnum):
    PURCHASE = "purchase"
    BANK_FX_CHARGES = "bank_fx_charges"
    TRAVEL_INSPECTION = "travel_inspection"
    TRANSPORT = "transport"
    EXPORT_PLATES_INSURANCE = "export_plates_insurance"
    CUSTOMS_BROKER = "customs_broker"
    IMPORT_DUTY = "import_duty"
    MOTOR_VEHICLE_TAX = "motor_vehicle_tax"
    IMPORT_VAT = "import_vat"
    OTHER_IMPORT_CHARGES = "other_import_charges"
    HOMOLOGATION_REGISTRATION = "homologation_registration"
    REPAIRS = "repairs"
    PREPARATION = "preparation"
    RISK_RESERVE = "risk_reserve"
    STORAGE_HOLDING = "storage_holding"
    SELLING_COSTS = "selling_costs"
    REFUNDABLE_DEPOSIT = "refundable_deposit"


class TaxRuleStatus(StrEnum):
    DRAFT = "draft"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    EXPIRED = "expired"
    REVOKED = "revoked"
    UNAPPROVED = "unapproved"  # example/fixture rule sets; never selectable for production


class ScenarioName(StrEnum):
    CONSERVATIVE = "conservative"
    BASE = "base"
    UPSIDE = "upside"


class FxPurpose(StrEnum):
    REFERENCE = "reference"
    CUSTOMS = "customs"
    PAYMENT = "payment"


class Role(StrEnum):
    OWNER = "owner"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Scope(StrEnum):
    DEALS_READ = "deals:read"
    REVIEWS_READ = "reviews:read"
    REVIEWS_WRITE = "reviews:write"
    EVENTS_SUBSCRIBE = "events:subscribe"
    RECHECKS_REQUEST = "rechecks:request"
    NOTES_WRITE = "notes:write"
    SOURCES_PAUSE = "sources:pause"
    CONFIG_ADMIN = "config:admin"
    # Spec v1.1 section 37.8: read-only inquiry/reply tools and the narrowly granted kill switch.
    INQUIRIES_READ = "inquiries:read"
    INQUIRIES_PAUSE = "inquiries:pause"
    # Mailbox-bound local reply worker: binding sync + correlated reply ingest only.
    MAIL_INGEST = "mail:ingest"


class ProfileKey(StrEnum):
    PRIMARY = "primary"
    MANUAL_4000 = "manual_4000"
    BELOW_TARGET_WATCH = "below_target_watch"


class GateStatus(StrEnum):
    """Honest completion states (spec section 32)."""

    NOT_REQUESTED = "not_requested"
    IMPLEMENTED = "implemented"
    FIXTURE_VERIFIED = "fixture_verified"
    INTEGRATION_VERIFIED = "integration_verified"
    LIVE_VERIFIED = "live_verified"
    ACTIVE = "active"
    BLOCKED = "blocked"


# --- Spec v1.1 section 37: bounded automatic seller inquiries ------------------------------


class InquiryReadiness(StrEnum):
    """Separate from investment readiness (spec 37.2)."""

    INQUIRY_READY = "inquiry_ready"
    NEEDS_FACTS = "needs_facts"
    NEEDS_TECHNICAL_REVIEW = "needs_technical_review"
    NOT_ELIGIBLE = "not_eligible"


class InquiryState(StrEnum):
    """Seller inquiry lifecycle (spec 37.5). `accepted` = provider accepted, not delivered/read."""

    CANDIDATE = "candidate"
    QUALIFYING = "qualifying"
    RESERVED = "reserved"
    QUEUED = "queued"
    SENDING = "sending"
    ACCEPTED = "accepted"
    HELD_FACTS = "held_facts"
    UNCERTAIN = "uncertain"
    SUPPRESSED = "suppressed"
    FAILED_DEFINITE = "failed_definite"
    CANCELLED = "cancelled"
    REPLIED = "replied"
    BOUNCED = "bounced"
    SELLER_OPTED_OUT = "seller_opted_out"
    NO_REPLY_YET = "no_reply_yet"


class SuppressionReason(StrEnum):
    HARD_BOUNCE = "hard_bounce"
    COMPLAINT = "complaint"
    SELLER_OPT_OUT = "seller_opt_out"
    SOURCE_PAUSED = "source_paused"
    SENDER_REVOKED = "sender_revoked"
    UNRESOLVED_SEND_OUTCOME = "unresolved_send_outcome"
    KILL_SWITCH = "kill_switch"
    CONTRADICTORY_AVAILABILITY = "contradictory_availability"
    MANUAL = "manual"
    #: The owner revoked the standing authorization (spec 37.1); distinct from the kill switch.
    AUTHORIZATION_REVOKED = "authorization_revoked"


class EmailProviderKind(StrEnum):
    OUTLOOK_LOCAL = "outlook_local"  # classic Outlook for Windows via the local worker
    GMAIL_API = "gmail_api"
    MICROSOFT_GRAPH = "microsoft_graph"


class MessageLanguage(StrEnum):
    """Supported inquiry template languages. English only with positive evidence (spec 37.3)."""

    DE = "de"
    IT = "it"
    FR = "fr"
    EN = "en"


class ReplyMessageType(StrEnum):
    SELLER_REPLY = "seller_reply"
    AUTO_REPLY = "auto_reply"
    BOUNCE = "bounce"
    DELIVERY_NOTICE = "delivery_notice"
    SPAM = "spam"
    AMBIGUOUS = "ambiguous"  # quarantined until verified


class AvailabilityEvidenceKind(StrEnum):
    """Evidence behind an availability event (spec 37.9). Never proves a purchase or price."""

    SOURCE_OBSERVATION = "source_observation"
    SOURCE_SOLD_BADGE = "source_sold_badge"
    SOURCE_REMOVED_PAGE = "source_removed_page"
    #: An explicit "reserved" badge on the source page -> ``reserved``.
    SOURCE_RESERVED_BADGE = "source_reserved_badge"
    #: A 404/not-found detail page from a healthy source -> ``unknown`` (never removed or sold).
    SOURCE_DETAIL_NOT_FOUND = "source_detail_not_found"
    SELLER_REPORTED_SOLD = "seller_reported_sold"
    SELLER_REPORTED_AVAILABLE = "seller_reported_available"
    SELLER_REPORTED_RESERVED = "seller_reported_reserved"
    COMPLETE_SCAN_ABSENCE = "complete_scan_absence"
    MANUAL = "manual"
