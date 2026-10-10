"""Exact-ad seller-contact evidence from a parsed page (spec 37.3; F1, wave D2). Pure, no I/O.

Only what the advertisement VISIBLY shows counts: ``mailto:`` links and e-mail addresses in the
page text. Hidden markup (script/JSON-LD/style) is never a source of a recipient: an address only
in structured data is not "shown on the advertisement". Every distinct address on the page is
counted, so a page showing several (branches, staff) can never yield a single recipient. A
contact form is reported as a form (spec: never submitted); a login/contact-reveal wall as a
restriction (never bypassed). The excerpt is the visible text around the address, bounded, so
the verification (``domain.seller_contacts.verify_recipient``) can check it really shows it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final
from urllib.parse import unquote, urlsplit

from suv_deals.adapters._extract import Anchor, PageView, collapse_ws
from suv_deals.adapters.base import AdText, SellerContactEvidence, ShownEmail, ShownEmailLocation
from suv_deals.domain.enums import SellerType

EXCERPT_CHARS: Final = 500
_WINDOW: Final = 120
#: Visible e-mail addresses (conservative dot-atom; the domain needs a dotted TLD).
_EMAIL_RE: Final = re.compile(
    r"(?<![A-Za-z0-9._%+'@-])([A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24})"
    r"(?![A-Za-z0-9_@-]|\.[A-Za-z0-9])"
)
_REVEAL_MARKERS: Final = (
    "e-mail anzeigen",
    "kontakt anzeigen",
    "contatti visibili dopo",
    "mostra email",
    "afficher l'e-mail",
    "afficher le contact",
    "show email",
    "log in to see",
    "anmelden, um",
)


def _addresses(text: str) -> list[str]:
    return [m.group(1) for m in _EMAIL_RE.finditer(text)]


def _mailto(anchor: Anchor) -> str | None:
    href = anchor.href.strip()
    if not href.lower().startswith("mailto:"):
        return None
    target = unquote(href[len("mailto:") :]).split("?", 1)[0].strip()
    found = _addresses(target)
    return found[0] if len(found) == 1 and found[0] == target else None


def _window(text: str, address: str) -> str | None:
    index = text.find(address)
    if index < 0:
        return None
    start = max(0, index - _WINDOW)
    end = min(len(text), index + len(address) + _WINDOW)
    return collapse_ws(text[start:end])[:EXCERPT_CHARS]


def _excerpt(address: str, anchors: Iterable[Anchor], visible: str) -> str | None:
    for anchor in anchors:
        if _mailto(anchor) == address:
            for candidate in (anchor.container_text, anchor.text):
                text = collapse_ws(candidate or "")
                if address in text:
                    return _window(text, address)
    return _window(visible, address)


def _site_origin(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.hostname}/"


def extract_seller_contact(
    page: PageView,
    *,
    seller_type: SellerType,
    seller_name: str | None,
    seller_reference: str | None,
    dealer_website: str | None,
    description: str | None,
    title: str | None,
    description_selector: str | None = None,
    seller_texts: Iterable[str] = (),
) -> SellerContactEvidence:
    """The contact evidence of one OK detail page (see the module docstring).

    ``description`` is the seller text the listing keeps (the language evidence);
    ``seller_texts`` are every visible seller-written text of the page (an address inside one of
    them is located ``listing_description``, anywhere else ``listing_contact_block``).
    """
    visible = page.visible_text
    mailtos = [a for a in page.anchors if _mailto(a) is not None]
    shown: list[str] = []
    for address in [*(_mailto(a) or "" for a in mailtos), *_addresses(visible)]:
        if address and address.lower() not in {s.lower() for s in shown}:
            shown.append(address)
    description_text = collapse_ws(description or "")
    seller_written = [collapse_ws(t) for t in (description_text, *seller_texts) if t]
    emails: list[ShownEmail] = []
    for address in shown[:20]:
        inside = next((text for text in seller_written if address in text), None)
        location: ShownEmailLocation = "listing_description" if inside else "listing_contact_block"
        excerpt = _window(inside, address) if inside else _excerpt(address, mailtos, visible)
        if excerpt and address in excerpt:
            emails.append(ShownEmail(address=address, location=location, excerpt=excerpt))
    lowered = page.text_lower
    texts: list[AdText] = []
    if description_text:
        texts.append(
            AdText(
                field="description",
                text=description_text[:4000],
                seller_written=True,
                selector=description_selector,
            )
        )
    if title:
        # A dealer site's vehicle title is usually composed from structured fields: recorded, not decisive.
        texts.append(AdText(field="title", text=title[:4000], seller_written=False, selector="title"))
    return SellerContactEvidence(
        seller_type=seller_type,
        seller_name=seller_name,
        seller_reference=seller_reference,
        dealer_website=dealer_website,
        emails=tuple(emails),
        distinct_addresses=len(shown),
        contact_form="<form" in page.raw_lower and not page.has_password_input,
        contact_reveal_restricted=any(marker in lowered for marker in _REVEAL_MARKERS),
        ad_texts=tuple(texts),
        page_language=page.lang,
    )


def site_origin(url: str) -> str | None:
    """``https://host/`` of a page URL (a dealer inventory site's own website)."""
    return _site_origin(url)


__all__ = ["EXCERPT_CHARS", "extract_seller_contact", "site_origin"]
