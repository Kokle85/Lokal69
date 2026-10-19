"""AES-256-GCM secret box for callback secrets at rest: tamper, AAD swap, rotation, config."""

from __future__ import annotations

import base64
import logging
from uuid import UUID

import pytest
from pydantic import SecretStr

from suv_deals.errors import AppError, ErrorCode
from suv_deals.integrations.secret_box import (
    FORMAT_VERSION,
    SecretBox,
    SecretBoxConfigError,
    SecretDecryptionFailed,
    parse_keyring,
    subscription_secret_aad,
)
from suv_deals.settings import Settings

WS = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
KEY1 = bytes(range(32))
KEY2 = bytes(range(100, 132))
B64_1 = base64.b64encode(KEY1).decode()
B64_2 = base64.b64encode(KEY2).decode()
SECRET = b"whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"


def _aad(sub: str = "sub_0123456789abcdef0123456789abcdef", version: int = 1) -> bytes:
    return subscription_secret_aad(workspace_id=WS, subscription_id=sub, secret_version=version)


def test_round_trip_and_envelope_layout() -> None:
    box = SecretBox({1: KEY1}, 1)
    envelope = box.seal(SECRET, aad=_aad())
    assert envelope[0] == FORMAT_VERSION
    assert envelope[1] == 1
    assert len(envelope) == 2 + 12 + len(SECRET) + 16
    assert SECRET not in envelope
    assert box.open(envelope, aad=_aad()) == SECRET


def test_nonce_is_fresh_per_seal() -> None:
    box = SecretBox({1: KEY1}, 1)
    assert box.seal(SECRET, aad=_aad()) != box.seal(SECRET, aad=_aad())


@pytest.mark.parametrize("position", [0, 1, 2, 13, 14, -1, -17])
def test_any_tampered_byte_fails(position: int) -> None:
    box = SecretBox({1: KEY1, 2: KEY2}, 1)
    envelope = bytearray(box.seal(SECRET, aad=_aad()))
    envelope[position] ^= 0x01
    with pytest.raises(SecretDecryptionFailed):
        box.open(bytes(envelope), aad=_aad())


def test_truncated_or_garbage_envelope_fails() -> None:
    box = SecretBox({1: KEY1}, 1)
    envelope = box.seal(SECRET, aad=_aad())
    for bad in (b"", envelope[:10], envelope[:29], b"\x02" + envelope[1:], b"x" * 40):
        with pytest.raises(SecretDecryptionFailed):
            box.open(bad, aad=_aad())


def test_aad_binding_prevents_swapping_between_rows() -> None:
    box = SecretBox({1: KEY1}, 1)
    row_a = box.seal(SECRET, aad=_aad("sub_aaaa"))
    with pytest.raises(SecretDecryptionFailed):
        box.open(row_a, aad=_aad("sub_bbbb"))
    with pytest.raises(SecretDecryptionFailed):
        box.open(row_a, aad=_aad("sub_aaaa", version=2))
    other_ws = subscription_secret_aad(
        workspace_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
        subscription_id="sub_aaaa",
        secret_version=1,
    )
    with pytest.raises(SecretDecryptionFailed):
        box.open(row_a, aad=other_ws)


def test_aad_is_required() -> None:
    box = SecretBox({1: KEY1}, 1)
    with pytest.raises(ValueError):
        box.seal(SECRET, aad=b"")


@pytest.mark.parametrize(("sub", "version"), [("", 1), ("s" * 129, 1), ("sub\x00x", 1), ("sub_ok", 0)])
def test_invalid_aad_inputs(sub: str, version: int) -> None:
    with pytest.raises(ValueError):
        subscription_secret_aad(workspace_id=WS, subscription_id=sub, secret_version=version)


def test_key_id_header_cannot_be_redirected_to_another_key() -> None:
    box = SecretBox({1: KEY1, 2: KEY2}, 1)
    envelope = bytearray(box.seal(SECRET, aad=_aad()))
    envelope[1] = 2  # claim it was sealed with key 2
    with pytest.raises(SecretDecryptionFailed):
        box.open(bytes(envelope), aad=_aad())


def test_rotation_decrypts_old_and_rewraps_with_current() -> None:
    old_box = SecretBox({1: KEY1}, 1)
    old = old_box.seal(SECRET, aad=_aad())
    rotated = SecretBox.from_config_value(f"2:{B64_2},1:{B64_1}")
    assert rotated.current_key_id == 2
    assert rotated.needs_rewrap(old)
    assert rotated.open(old, aad=_aad()) == SECRET
    new = rotated.rewrap(old, aad=_aad())
    assert new[1] == 2
    assert not rotated.needs_rewrap(new)
    assert rotated.open(new, aad=_aad()) == SECRET
    retired = SecretBox.from_config_value(f"2:{B64_2}")
    assert retired.open(new, aad=_aad()) == SECRET
    with pytest.raises(SecretDecryptionFailed, match="not configured"):
        retired.open(old, aad=_aad())


def test_text_helpers_round_trip_and_return_secretstr() -> None:
    box = SecretBox({1: KEY1}, 1)
    token = box.seal_text("whsec_abc", aad=_aad())
    assert token.startswith("sbx1:")
    assert "whsec" not in token
    opened = box.open_text(token, aad=_aad())
    assert isinstance(opened, SecretStr)
    assert opened.get_secret_value() == "whsec_abc"
    for bad in ("", "sbx1:", "sbx1:!!!", "sbx2:" + token[5:], token[:-4]):
        with pytest.raises(SecretDecryptionFailed):
            box.open_text(bad, aad=_aad())


def test_plaintext_bounds() -> None:
    box = SecretBox({1: KEY1}, 1)
    with pytest.raises(ValueError):
        box.seal(b"", aad=_aad())
    with pytest.raises(ValueError):
        box.seal(b"x" * 4097, aad=_aad())
    with pytest.raises(TypeError):
        box.seal("text", aad=_aad())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- key configuration


def test_single_key_value_is_key_id_one() -> None:
    keys, current = parse_keyring(B64_1)
    assert keys == {1: KEY1}
    assert current == 1


def test_urlsafe_and_unpadded_keys_accepted() -> None:
    urlsafe = base64.urlsafe_b64encode(bytes([251] * 32)).decode().rstrip("=")
    keys, _ = parse_keyring(urlsafe)
    assert keys[1] == bytes([251] * 32)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        base64.b64encode(b"x" * 31).decode(),
        base64.b64encode(b"x" * 33).decode(),
        base64.b64encode(b"x" * 16).decode(),
        "not base64 at all!",
        f"0:{B64_1}",
        f"256:{B64_1}",
        f"1:{B64_1},1:{B64_2}",
        f"1:{B64_1},garbage",
        f"x:{B64_1}",
    ],
)
def test_invalid_key_configuration_rejected(value: str) -> None:
    with pytest.raises(SecretBoxConfigError) as info:
        parse_keyring(value)
    assert isinstance(info.value, AppError)
    assert info.value.code is ErrorCode.INTERNAL_ERROR
    assert B64_1 not in info.value.message


def test_constructor_validation() -> None:
    with pytest.raises(SecretBoxConfigError):
        SecretBox({}, 1)
    with pytest.raises(SecretBoxConfigError):
        SecretBox({1: b"short"}, 1)
    with pytest.raises(SecretBoxConfigError):
        SecretBox({1: KEY1}, 2)
    with pytest.raises(SecretBoxConfigError):
        SecretBox({300: KEY1}, 300)


def test_from_settings_requires_key() -> None:
    with pytest.raises(SecretBoxConfigError):
        SecretBox.from_settings(Settings(_env_file=None))  # type: ignore[call-arg]
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        mcp_event_subscription_secret_encryption_key=SecretStr(B64_1),
    )
    box = SecretBox.from_settings(settings)
    assert box.open(box.seal(SECRET, aad=_aad()), aad=_aad()) == SECRET


def test_repr_and_errors_never_contain_key_or_plaintext(caplog: pytest.LogCaptureFixture) -> None:
    box = SecretBox({1: KEY1}, 1)
    assert B64_1 not in repr(box)
    assert "key_ids=[1]" in repr(box)
    envelope = box.seal(SECRET, aad=_aad())
    with caplog.at_level(logging.DEBUG), pytest.raises(SecretDecryptionFailed) as info:
        box.open(envelope, aad=_aad("sub_other"))
    assert SECRET.decode() not in str(info.value)
    assert caplog.records == []
