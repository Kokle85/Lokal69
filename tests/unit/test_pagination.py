"""Unit tests for domain.pagination (spec 21 "Pagination and errors"). Secrets and ids are SYNTHETIC."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest

from suv_deals.domain import pagination as pagination_module
from suv_deals.domain.enums import ReviewState
from suv_deals.domain.pagination import (
    MAX_CURSOR_LENGTH,
    CursorPayload,
    decode_cursor,
    encode_cursor,
    filter_hash,
    keyset_cursor,
    next_snapshot_ordinal,
    snapshot_cursor,
    sort_value_of,
    validate_limit,
)
from suv_deals.errors import ErrorCode, ValidationFailed

NOW = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
SECRET = b"synthetic-cursor-secret-0123456789abcdef"
OLD_SECRET = b"synthetic-previous-secret-0123456789abcdef"
WS = UUID(int=100)
ALICE = UUID(int=1)
BOB = UUID(int=2)
SNAP = UUID(int=500)
FILTERS = {"include_needs_information": True, "limit": 25, "cursor": None}
FH = filter_hash(FILTERS)


def snap_payload(**kw: Any) -> CursorPayload:
    data: dict[str, Any] = {
        "query": "reviews_list_pending",
        "workspace_id": WS,
        "principal_id": ALICE,
        "filters_hash": FH,
        "snapshot_id": SNAP,
        "next_ordinal": 25,
        "now": NOW,
        "snapshot_expires_at": NOW + timedelta(hours=1),
    }
    data.update(kw)
    return snapshot_cursor(**data)


def decode(token: object, **kw: Any) -> CursorPayload:
    data: dict[str, Any] = {
        "query": "reviews_list_pending",
        "workspace_id": WS,
        "principal_id": ALICE,
        "filters_hash": FH,
        "now": NOW + timedelta(minutes=1),
    }
    data.update(kw)
    secret = data.pop("secret", SECRET)
    return decode_cursor(token, secret, **data)


def reason(exc: pytest.ExceptionInfo[ValidationFailed]) -> str:
    assert exc.value.code == ErrorCode.VALIDATION_ERROR
    return str(exc.value.details["cursor"])


def test_snapshot_round_trip() -> None:
    payload = snap_payload()
    token = encode_cursor(payload, SECRET)
    assert len(token) <= MAX_CURSOR_LENGTH
    assert "." in token and "=" not in token
    decoded = decode(token)
    assert decoded == payload
    assert decoded.mode == "snapshot" and decoded.ord == 25 and decoded.snap == SNAP


def test_keyset_round_trip_with_unique_tiebreaker() -> None:
    last = (ReviewState.PENDING, 1200, datetime(2026, 10, 6, 9, 0, tzinfo=UTC), UUID(int=77))
    payload = keyset_cursor(
        query="deals_list_candidates",
        workspace_id=WS,
        principal_id=ALICE,
        filters_hash=FH,
        last_sort_key=last,
        as_of=NOW,
        now=NOW,
    )
    assert payload.sort == ("pending", 1200, "2026-10-06T09:00:00Z", str(UUID(int=77)))
    token = encode_cursor(payload, SECRET)
    assert decode(token, query="deals_list_candidates") == payload


def test_keyset_requires_unique_id_last() -> None:
    with pytest.raises(ValidationFailed):
        keyset_cursor(
            query="deals_list_candidates",
            workspace_id=WS,
            principal_id=ALICE,
            filters_hash=FH,
            last_sort_key=(1200, "2026-10-06"),
            as_of=NOW,
            now=NOW,
        )
    with pytest.raises(ValidationFailed):
        keyset_cursor(
            query="deals_list_candidates",
            workspace_id=WS,
            principal_id=ALICE,
            filters_hash=FH,
            last_sort_key=(),
            as_of=NOW,
            now=NOW,
        )


def test_tampered_body_rejected() -> None:
    token = encode_cursor(snap_payload(), SECRET)
    body, sig = token.split(".")
    raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    raw["ord"] = 0  # try to rewind/skip pages
    forged = base64.urlsafe_b64encode(json.dumps(raw).encode()).rstrip(b"=").decode()
    with pytest.raises(ValidationFailed) as exc:
        decode(f"{forged}.{sig}")
    assert reason(exc) == "tampered"


def test_tampered_signature_and_wrong_secret_rejected() -> None:
    token = encode_cursor(snap_payload(), SECRET)
    body, sig = token.split(".")
    flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    with pytest.raises(ValidationFailed) as exc:
        decode(f"{body}.{flipped}")
    assert reason(exc) == "tampered"
    with pytest.raises(ValidationFailed) as exc2:
        decode(token, secret=b"x" * 40)
    assert reason(exc2) == "tampered"


def test_secret_rotation_accepts_previous_secret() -> None:
    token = encode_cursor(snap_payload(), OLD_SECRET)
    assert decode(token, secret=[SECRET, OLD_SECRET]).ord == 25
    with pytest.raises(ValidationFailed):
        decode(token, secret=[SECRET])


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "no-dot",
        "a.b.c",
        "abc.d=f",
        "ab$c.def",
        "x" * (MAX_CURSOR_LENGTH + 1),
        42,
    ],
)
def test_malformed_tokens_rejected(token: object) -> None:
    with pytest.raises(ValidationFailed) as exc:
        decode(token)
    assert reason(exc) in {"malformed", "tampered"}


def test_validly_signed_garbage_body_rejected() -> None:
    body = base64.urlsafe_b64encode(b'{"v":1,"q":"x"}').rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(pagination_module._mac(SECRET, body)).rstrip(b"=").decode()
    with pytest.raises(ValidationFailed) as exc:
        decode(f"{body}.{sig}")
    assert reason(exc) == "malformed"


def test_expired_cursor_rejected() -> None:
    token = encode_cursor(snap_payload(ttl=timedelta(minutes=5)), SECRET)
    assert decode(token, now=NOW + timedelta(minutes=4, seconds=59)).ord == 25
    with pytest.raises(ValidationFailed) as exc:
        decode(token, now=NOW + timedelta(minutes=5))
    assert reason(exc) == "expired"


def test_cursor_never_outlives_snapshot() -> None:
    payload = snap_payload(snapshot_expires_at=NOW + timedelta(minutes=2))
    assert payload.exp == NOW + timedelta(minutes=2)
    with pytest.raises(ValidationFailed):
        snap_payload(snapshot_expires_at=NOW)


def test_future_issued_cursor_rejected() -> None:
    token = encode_cursor(
        snap_payload(now=NOW + timedelta(hours=2), snapshot_expires_at=NOW + timedelta(hours=3)), SECRET
    )
    with pytest.raises(ValidationFailed) as exc:
        decode(token)
    assert reason(exc) == "malformed"


@pytest.mark.parametrize(
    "override",
    [
        {"workspace_id": UUID(int=999)},
        {"principal_id": BOB},
        {"query": "deals_list_candidates"},
        {"filters_hash": filter_hash({"include_needs_information": False})},
    ],
)
def test_mismatched_binding_rejected(override: dict[str, Any]) -> None:
    token = encode_cursor(snap_payload(), SECRET)
    with pytest.raises(ValidationFailed) as exc:
        decode(token, **override)
    assert reason(exc) == "mismatch"


def test_filter_hash_canonical() -> None:
    assert filter_hash({"a": 1, "b": "x"}) == filter_hash({"b": "x", "a": 1})
    assert filter_hash({"a": 1, "cursor": "abc", "limit": 50}) == filter_hash({"a": 1})
    assert filter_hash({"a": 1, "country": None}) == filter_hash({"a": 1})
    assert filter_hash({"status": ReviewState.PENDING}) == filter_hash({"status": "pending"})
    assert filter_hash({"a": 1}) != filter_hash({"a": 2})


def test_sort_value_normalisation() -> None:
    assert sort_value_of(Decimal("2750.50")) == "2750.50"
    assert sort_value_of(UUID(int=1)) == str(UUID(int=1))
    assert sort_value_of(True) is True
    with pytest.raises(ValidationFailed):
        sort_value_of(1.5)
    with pytest.raises(ValidationFailed):
        sort_value_of(datetime(2026, 10, 6))


def test_cursor_lifetime_bounded() -> None:
    with pytest.raises(ValidationFailed):
        snap_payload(ttl=timedelta(days=2), snapshot_expires_at=NOW + timedelta(days=3))
    with pytest.raises(ValidationFailed):
        snap_payload(ttl=timedelta(0))


def test_snapshot_ordinal_bounds() -> None:
    with pytest.raises(ValidationFailed):
        snap_payload(next_ordinal=-1)
    with pytest.raises(ValidationFailed):
        snap_payload(next_ordinal=10_001)


def test_short_secret_rejected() -> None:
    with pytest.raises(ValidationFailed):
        encode_cursor(snap_payload(), b"short")
    with pytest.raises(ValidationFailed):
        encode_cursor(snap_payload(), [])
    with pytest.raises(ValidationFailed):
        decode("abc.def", secret=b"short")


def test_oversized_cursor_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pagination_module, "MAX_CURSOR_LENGTH", 100)
    with pytest.raises(ValidationFailed):
        encode_cursor(snap_payload(), SECRET)


def test_validate_limit() -> None:
    assert validate_limit(None) == 25
    assert validate_limit(1) == 1 and validate_limit(100) == 100
    for bad in (0, 101, -1, True):
        with pytest.raises(ValidationFailed):
            validate_limit(bad)


def test_next_snapshot_ordinal() -> None:
    assert next_snapshot_ordinal(0, 25, 60) == 25
    assert next_snapshot_ordinal(50, 10, 60) is None
    with pytest.raises(ValidationFailed):
        next_snapshot_ordinal(50, 20, 60)


def test_naive_times_rejected() -> None:
    with pytest.raises(ValidationFailed):
        snap_payload(now=datetime(2026, 10, 6, 10, 0))
