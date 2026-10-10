"""Standard Webhooks signing/verification (spec 22; docs/research/mcp_events_and_webhooks.md 9-11)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from freezegun import freeze_time
from standardwebhooks import Webhook

from suv_deals.errors import ErrorCode
from suv_deals.integrations.webhook_signing import (
    HEADER_ID,
    HEADER_SIGNATURE,
    HEADER_SUBSCRIPTION,
    HEADER_TIMESTAMP,
    WEBHOOK_BODY_LIMIT_BYTES,
    InvalidWebhookSecret,
    VerificationFailureReason,
    WebhookPayloadTooLarge,
    WebhookReplayGuard,
    WebhookSecret,
    WebhookVerificationFailed,
    build_signed_request,
    canonical_json,
    parse_whsec,
    sign,
    validate_webhook_id,
    verify_inbound,
)

# Standard Webhooks specification test vector (reproduced with the Python library).
SPEC_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SPEC_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SPEC_TS = 1614265330
SPEC_BODY = '{"test": 2432232314}'
SPEC_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

NOW = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)


def _whsec(byte: int, length: int = 32) -> str:
    return "whsec_" + base64.b64encode(bytes([byte]) * length).decode()


SECRET_A = parse_whsec(_whsec(0xA1))
SECRET_B = parse_whsec(_whsec(0xB2, 48))


def _headers(webhook_id: str, ts: int, signature: str) -> dict[str, str]:
    return {HEADER_ID: webhook_id, HEADER_TIMESTAMP: str(ts), HEADER_SIGNATURE: signature}


# --------------------------------------------------------------------------- spec vector


def test_spec_test_vector_signature() -> None:
    secret = parse_whsec(SPEC_SECRET)
    signed_at = datetime.fromtimestamp(SPEC_TS, tz=UTC)
    assert sign(secret, SPEC_ID, signed_at, SPEC_BODY) == SPEC_SIGNATURE


def test_spec_test_vector_verifies_inbound() -> None:
    secret = parse_whsec(SPEC_SECRET)
    with freeze_time(datetime.fromtimestamp(SPEC_TS + 30, tz=UTC)):
        verified = verify_inbound(secret, SPEC_BODY.encode(), _headers(SPEC_ID, SPEC_TS, SPEC_SIGNATURE))
    assert verified.payload == {"test": 2432232314}
    assert verified.webhook_id == SPEC_ID
    assert verified.timestamp == datetime.fromtimestamp(SPEC_TS, tz=UTC)


def test_non_utc_aware_timestamp_is_converted_not_relabelled() -> None:
    skopje = datetime.fromtimestamp(SPEC_TS, tz=UTC).astimezone(ZoneInfo("Europe/Skopje"))
    assert sign(parse_whsec(SPEC_SECRET), SPEC_ID, skopje, SPEC_BODY) == SPEC_SIGNATURE


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError, match="naive"):
        sign(SECRET_A, "msg_1", datetime(2026, 10, 6, 10, 0), "{}")


def test_bytes_body_rejected_because_library_would_sign_repr() -> None:
    with pytest.raises(TypeError):
        sign(SECRET_A, "msg_1", NOW, b"{}")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- secret parsing


@pytest.mark.parametrize("length", [24, 32, 64])
def test_parse_whsec_accepts_24_to_64_bytes(length: int) -> None:
    secret = parse_whsec(_whsec(7, length))
    assert len(secret.key) == length


def test_parse_whsec_accepts_unpadded_base64() -> None:
    padded = _whsec(9, 25)
    assert padded.endswith("=")
    assert parse_whsec(padded.rstrip("=")).key == parse_whsec(padded).key


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "",
        "whsec_",
        "MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",  # missing prefix
        "WHSEC_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaS!",  # invalid character (the library would drop it)
        "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw ",
        "whsec_MfKQ9r8G KYqrTwjUPD8ILPZIo2LaLaSw",
        "whsec_" + base64.b64encode(b"x" * 23).decode(),  # 23 bytes: too short
        "whsec_" + base64.b64encode(b"x" * 65).decode(),  # 65 bytes: too long
        "whsec_AAA",  # 2 bytes
        "whsec_" + "A" * 200,
        "whsk_" + base64.b64encode(b"x" * 32).decode(),  # asymmetric keys are not accepted here
    ],
)
def test_parse_whsec_rejects_invalid(value: object) -> None:
    with pytest.raises(InvalidWebhookSecret) as info:
        parse_whsec(value)
    assert info.value.code is ErrorCode.VALIDATION_ERROR
    if isinstance(value, str) and len(value) > 6:
        assert value[6:] not in info.value.message


def test_secret_repr_and_fingerprint_never_reveal_key() -> None:
    text = repr(SECRET_A) + str(SECRET_A)
    assert "<redacted>" in text
    assert base64.b64encode(SECRET_A.key).decode() not in text
    assert SECRET_A.fingerprint == parse_whsec(_whsec(0xA1)).fingerprint
    assert SECRET_A.fingerprint != SECRET_B.fingerprint
    assert SECRET_A.matches(parse_whsec(_whsec(0xA1)))
    assert not SECRET_A.matches(SECRET_B)
    assert parse_whsec(SECRET_A.to_whsec()).matches(SECRET_A)


def test_webhook_secret_enforces_length_directly() -> None:
    with pytest.raises(InvalidWebhookSecret):
        WebhookSecret(b"short")


# --------------------------------------------------------------------------- ids


@pytest.mark.parametrize(
    "webhook_id", ["msg.1", "", "a b", "msg_\n", "x" * 256, "msg_é", None, "33333333.3333"]
)
def test_invalid_webhook_ids_rejected(webhook_id: object) -> None:
    with pytest.raises(ValueError):
        validate_webhook_id(webhook_id)


def test_uuid_and_verification_ids_are_valid() -> None:
    assert validate_webhook_id("33333333-3333-4333-8333-333333333333")
    assert validate_webhook_id("msg_verification_Ab-_09")


def test_sign_rejects_dotted_id() -> None:
    with pytest.raises(ValueError):
        sign(SECRET_A, "msg.with.dot", NOW, "{}")


# --------------------------------------------------------------------------- rotation


def test_rotation_signs_with_every_secret_newest_first() -> None:
    header = sign([SECRET_B, SECRET_A], "msg_rot", NOW, '{"a":1}')
    entries = header.split(" ")
    assert len(entries) == 2
    assert entries[0] == Webhook(SECRET_B.key).sign("msg_rot", NOW, '{"a":1}')
    assert entries[1] == Webhook(SECRET_A.key).sign("msg_rot", NOW, '{"a":1}')


def test_rotation_deduplicates_identical_secrets() -> None:
    header = sign([SECRET_A, parse_whsec(_whsec(0xA1))], "msg_rot", NOW, "{}")
    assert len(header.split(" ")) == 1


def test_sign_requires_at_least_one_secret() -> None:
    with pytest.raises(ValueError):
        sign([], "msg_x", NOW, "{}")


@freeze_time(NOW)
def test_receiver_with_either_rotated_key_verifies() -> None:
    signed = build_signed_request([SECRET_B, SECRET_A], "evt_rot", "sub_abc", {"x": 1}, now=NOW)
    assert verify_inbound(SECRET_A, signed.body, signed.headers).payload == {"x": 1}
    assert verify_inbound(SECRET_B, signed.body, signed.headers).payload == {"x": 1}
    other = parse_whsec(_whsec(0x55))
    with pytest.raises(WebhookVerificationFailed) as info:
        verify_inbound(other, signed.body, signed.headers)
    assert info.value.reason is VerificationFailureReason.NO_MATCHING_SIGNATURE


@freeze_time(NOW)
def test_verifier_accepts_any_of_several_configured_secrets() -> None:
    signed = build_signed_request(SECRET_B, "evt_multi", "sub_abc", {"x": 1}, now=NOW)
    assert verify_inbound([SECRET_A, SECRET_B], signed.body, signed.headers).payload == {"x": 1}


# --------------------------------------------------------------------------- build_signed_request


def test_build_signed_request_headers_and_single_serialization() -> None:
    payload = {"name": "review.pending.v1", "eventId": "e1", "data": {"b": 2, "a": "ü"}, "cursor": None}
    signed = build_signed_request(SECRET_A, "evt_1", "sub_0123", payload, now=NOW)
    assert signed.body == canonical_json(payload).encode("utf-8")
    assert signed.body.startswith(b'{"cursor":null,"data":{"a":"\xc3\xbc","b":2}')
    assert signed.headers["Content-Type"] == "application/json"
    assert signed.headers[HEADER_ID] == "evt_1"
    assert signed.headers[HEADER_TIMESTAMP] == str(int(NOW.timestamp()))
    assert signed.headers[HEADER_SUBSCRIPTION] == "sub_0123"
    expected = Webhook(SECRET_A.key).sign("evt_1", NOW, signed.body.decode("utf-8"))
    assert signed.headers[HEADER_SIGNATURE] == expected


def test_build_signed_request_key_order_independent() -> None:
    one = build_signed_request(SECRET_A, "evt_1", "sub_1", {"a": 1, "b": 2}, now=NOW)
    two = build_signed_request(SECRET_A, "evt_1", "sub_1", {"b": 2, "a": 1}, now=NOW)
    assert one.body == two.body
    assert one.headers == two.headers


def test_fresh_timestamp_changes_signature_but_not_id() -> None:
    first = build_signed_request(SECRET_A, "evt_1", "sub_1", {"a": 1}, now=NOW)
    later = build_signed_request(SECRET_A, "evt_1", "sub_1", {"a": 1}, now=NOW + timedelta(seconds=31))
    assert first.headers[HEADER_ID] == later.headers[HEADER_ID]
    assert first.headers[HEADER_TIMESTAMP] != later.headers[HEADER_TIMESTAMP]
    assert first.headers[HEADER_SIGNATURE] != later.headers[HEADER_SIGNATURE]


def test_body_limit_is_exactly_256_kib() -> None:
    overhead = len(canonical_json({"p": ""}).encode())
    at_limit = {"p": "x" * (WEBHOOK_BODY_LIMIT_BYTES - overhead)}
    signed = build_signed_request(SECRET_A, "evt_big", "sub_1", at_limit, now=NOW)
    assert len(signed.body) == WEBHOOK_BODY_LIMIT_BYTES == 262_144
    over = {"p": "x" * (WEBHOOK_BODY_LIMIT_BYTES - overhead + 1)}
    with pytest.raises(WebhookPayloadTooLarge) as info:
        build_signed_request(SECRET_A, "evt_big", "sub_1", over, now=NOW)
    assert info.value.details["size_bytes"] == 262_145


def test_multibyte_characters_count_as_bytes() -> None:
    overhead = len(canonical_json({"p": ""}).encode())
    chars = (WEBHOOK_BODY_LIMIT_BYTES - overhead) // 2 + 1  # 2-byte UTF-8 characters
    with pytest.raises(WebhookPayloadTooLarge):
        build_signed_request(SECRET_A, "evt_big", "sub_1", {"p": "é" * chars}, now=NOW)


@pytest.mark.parametrize("sub_id", ["", "sub.1", "sub 1", "s" * 129])
def test_invalid_subscription_ids_rejected(sub_id: str) -> None:
    with pytest.raises(ValueError):
        build_signed_request(SECRET_A, "evt_1", sub_id, {}, now=NOW)


def test_non_finite_numbers_are_not_serialized() -> None:
    with pytest.raises(ValueError):
        build_signed_request(SECRET_A, "evt_1", "sub_1", {"x": float("nan")}, now=NOW)


# --------------------------------------------------------------------------- verify_inbound failures


def _signed(now: datetime = NOW, payload: object | None = None) -> tuple[bytes, dict[str, str]]:
    signed = build_signed_request(SECRET_A, "evt_v", "sub_1", payload or {"ok": True}, now=now)
    return signed.body, dict(signed.headers)


def _reason(
    raw: bytes, headers: dict[str, str], secret: WebhookSecret = SECRET_A
) -> VerificationFailureReason:
    with pytest.raises(WebhookVerificationFailed) as info:
        verify_inbound(secret, raw, headers)
    assert info.value.code is ErrorCode.UNAUTHENTICATED
    assert info.value.message == "Webhook signature verification failed"
    return info.value.reason


@freeze_time(NOW)
def test_valid_request_verifies_with_case_insensitive_headers() -> None:
    raw, headers = _signed()
    upper = {k.upper(): v for k, v in headers.items()}
    verified = verify_inbound(SECRET_A, raw, upper)
    assert verified.payload == {"ok": True}
    assert verified.subscription_id == "sub_1"


@freeze_time(NOW)
def test_bad_signature_rejected() -> None:
    raw, headers = _signed()
    headers[HEADER_SIGNATURE] = "v1," + base64.b64encode(b"\x00" * 32).decode()
    assert _reason(raw, headers) is VerificationFailureReason.NO_MATCHING_SIGNATURE


@freeze_time(NOW)
def test_tampered_body_rejected() -> None:
    raw, headers = _signed()
    assert _reason(raw.replace(b"true", b"false"), headers) is VerificationFailureReason.NO_MATCHING_SIGNATURE


@freeze_time(NOW)
def test_reserialized_body_rejected() -> None:
    raw, headers = _signed(payload={"a": 1, "b": 2})
    pretty = json.dumps(json.loads(raw), indent=2).encode()
    assert _reason(pretty, headers) is VerificationFailureReason.NO_MATCHING_SIGNATURE


@freeze_time(NOW)
def test_stale_timestamp_rejected() -> None:
    raw, headers = _signed(now=NOW - timedelta(minutes=5, seconds=1))
    assert _reason(raw, headers) is VerificationFailureReason.STALE_TIMESTAMP


@freeze_time(NOW)
def test_timestamp_within_tolerance_accepted() -> None:
    raw, headers = _signed(now=NOW - timedelta(minutes=4, seconds=59))
    assert verify_inbound(SECRET_A, raw, headers).payload == {"ok": True}


@freeze_time(NOW)
def test_future_timestamp_rejected() -> None:
    raw, headers = _signed(now=NOW + timedelta(minutes=5, seconds=1))
    assert _reason(raw, headers) is VerificationFailureReason.FUTURE_TIMESTAMP


@freeze_time(NOW)
@pytest.mark.parametrize("missing", [HEADER_ID, HEADER_TIMESTAMP, HEADER_SIGNATURE])
def test_missing_headers_rejected(missing: str) -> None:
    raw, headers = _signed()
    del headers[missing]
    assert _reason(raw, headers) is VerificationFailureReason.MISSING_HEADERS


@freeze_time(NOW)
def test_empty_header_value_rejected() -> None:
    raw, headers = _signed()
    headers[HEADER_SIGNATURE] = ""
    assert _reason(raw, headers) is VerificationFailureReason.MISSING_HEADERS


@freeze_time(NOW)
@pytest.mark.parametrize(
    "signature",
    [
        "v1abc",  # no comma: the library would raise a bare ValueError
        "v1,",
        "v1,abc!",
        "v1,abc v1,def",  # double space -> empty entry
        " v1,abc",
        "v1,abc,def",
        ",abc",
        "sha256=abcdef",
    ],
)
def test_malformed_signature_entries_rejected(signature: str) -> None:
    raw, headers = _signed()
    headers[HEADER_SIGNATURE] = signature
    assert _reason(raw, headers) is VerificationFailureReason.MALFORMED_SIGNATURE


@freeze_time(NOW)
def test_bad_base64_padding_entry_maps_to_typed_failure() -> None:
    raw, headers = _signed()
    # "abcde" passes the character check but has impossible padding: the library raises
    # a bare binascii.Error (ValueError), which must surface as a typed failure.
    headers[HEADER_SIGNATURE] = "v1,abcde " + headers[HEADER_SIGNATURE]
    assert _reason(raw, headers) is VerificationFailureReason.MALFORMED_SIGNATURE


@freeze_time(NOW)
def test_v1a_entries_ignored_but_valid_v1_accepted() -> None:
    raw, headers = _signed()
    headers[HEADER_SIGNATURE] = "v1a,aGVsbG8= " + headers[HEADER_SIGNATURE]
    assert verify_inbound(SECRET_A, raw, headers).payload == {"ok": True}


@freeze_time(NOW)
@pytest.mark.parametrize("timestamp", ["abc", "-1", "1.5", "\uff11\uff12\uff13", "9" * 13, ""])
def test_malformed_timestamp_rejected(timestamp: str) -> None:
    raw, headers = _signed()
    headers[HEADER_TIMESTAMP] = timestamp
    expected = VerificationFailureReason.MISSING_HEADERS if not timestamp else None
    reason = _reason(raw, headers)
    assert reason is (expected or VerificationFailureReason.MALFORMED_TIMESTAMP)


@freeze_time(NOW)
def test_dotted_webhook_id_rejected_before_signature_check() -> None:
    raw, headers = _signed()
    headers[HEADER_ID] = "evt.v"
    assert _reason(raw, headers) is VerificationFailureReason.INVALID_ID


@freeze_time(NOW)
def test_oversized_body_rejected_before_verification() -> None:
    _, headers = _signed()
    assert _reason(b"x" * (WEBHOOK_BODY_LIMIT_BYTES + 1), headers) is VerificationFailureReason.BODY_TOO_LARGE


@freeze_time(NOW)
def test_non_utf8_body_rejected() -> None:
    _, headers = _signed()
    assert _reason(b"\xff\xfe{}", headers) is VerificationFailureReason.INVALID_BODY


@freeze_time(NOW)
def test_signed_but_invalid_json_rejected_after_verification() -> None:
    body = "{not json"
    ts = int(NOW.timestamp())
    headers = _headers("evt_bad", ts, sign(SECRET_A, "evt_bad", NOW, body))
    assert _reason(body.encode(), headers) is VerificationFailureReason.INVALID_BODY


@freeze_time(NOW)
def test_signed_nan_json_rejected() -> None:
    body = '{"x": NaN}'
    headers = _headers("evt_nan", int(NOW.timestamp()), sign(SECRET_A, "evt_nan", NOW, body))
    assert _reason(body.encode(), headers) is VerificationFailureReason.INVALID_BODY


def test_raw_body_must_be_bytes() -> None:
    with pytest.raises(TypeError):
        verify_inbound(SECRET_A, "{}", {})  # type: ignore[arg-type]


# --------------------------------------------------------------------------- replay protection


@freeze_time(NOW)
def test_replay_guard_rejects_second_delivery_of_same_id() -> None:
    guard = WebhookReplayGuard()
    raw, headers = _signed()
    verify_inbound(SECRET_A, raw, headers, replay_guard=guard)
    assert _reason_with_guard(raw, headers, guard) is VerificationFailureReason.REPLAYED


def _reason_with_guard(
    raw: bytes, headers: dict[str, str], guard: WebhookReplayGuard
) -> VerificationFailureReason:
    with pytest.raises(WebhookVerificationFailed) as info:
        verify_inbound(SECRET_A, raw, headers, replay_guard=guard)
    return info.value.reason


@freeze_time(NOW)
def test_forged_request_does_not_poison_replay_guard() -> None:
    guard = WebhookReplayGuard()
    raw, headers = _signed()
    forged = {**headers, HEADER_SIGNATURE: "v1," + base64.b64encode(b"\x01" * 32).decode()}
    with pytest.raises(WebhookVerificationFailed):
        verify_inbound(SECRET_A, raw, forged, replay_guard=guard)
    assert len(guard) == 0
    verify_inbound(SECRET_A, raw, headers, replay_guard=guard)


def test_replay_guard_expiry_and_bound() -> None:
    guard = WebhookReplayGuard(ttl=timedelta(minutes=10), max_entries=3)
    assert guard.check_and_record("a", now=NOW)
    assert not guard.check_and_record("a", now=NOW + timedelta(minutes=9))
    assert guard.check_and_record("a", now=NOW + timedelta(minutes=11))
    for name in ("b", "c", "d"):
        assert guard.check_and_record(name, now=NOW + timedelta(minutes=12))
    assert len(guard) == 3
