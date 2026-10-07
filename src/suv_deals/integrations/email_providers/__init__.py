"""Seller-email provider adapters (spec 37.3, 37.5, 37.8).

``base`` defines the ``SenderProvider`` contract and outcome models; ``gmail_api`` and
``microsoft_graph`` are direct API adapters (httpx, injected token provider); ``outlook_local``
holds the backend-side intent/report models for the classic-Outlook desktop worker route.
Construct providers only through ``suv_deals.integrations.seller_email`` so the sending
prerequisites (automatic mode, verified binding, kill switch off) are always enforced.
"""

from suv_deals.integrations.email_providers.base import (
    AttemptOutcomeMapping,
    ProviderCapabilities,
    ProviderHealth,
    ReconcileFoundSent,
    ReconcileNotFoundYet,
    ReconcileProviderUnavailable,
    ReplyFetchResult,
    SendAccepted,
    SendDefiniteFailure,
    SenderProvider,
    SenderVerification,
    SendUncertain,
    attempt_outcome,
    reconciliation_evidence,
)

__all__ = [
    "AttemptOutcomeMapping",
    "ProviderCapabilities",
    "ProviderHealth",
    "ReconcileFoundSent",
    "ReconcileNotFoundYet",
    "ReconcileProviderUnavailable",
    "ReplyFetchResult",
    "SendAccepted",
    "SendDefiniteFailure",
    "SendUncertain",
    "SenderProvider",
    "SenderVerification",
    "attempt_outcome",
    "reconciliation_evidence",
]
