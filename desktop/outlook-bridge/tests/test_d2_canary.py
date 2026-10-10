"""The desktop side of the ``outlook_local`` activation canary (F3, wave D2).

FakeOutlook + FakeBackend only (synthetic ``example.invalid`` addresses); nothing is sent anywhere.

- The canary is sent ONCE to the locally configured owner-controlled address whose hash is the
  canary's target hash, after a fresh claim; Sent Items evidence is reported afterwards.
- Refused before any ``.Send`` (and reported, so the canary fails honestly): no configured target,
  a target with another hash, the sending account as target, another mailbox, another sender
  account, an expired canary. The kill switch only defers it.
- An attempt interrupted after ``attempting`` is never sent again.
- Only a reply that names a canary THIS worker sent is reported, with headers and the sender's
  hash only; other mail (and other canary ids) is ignored.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from bridge_support import MAILBOX_ID, OTHER_MAILBOX_ID, OWNER, Harness
from outlook_bridge.canary import CanaryProcessor
from outlook_bridge.config import parse_config
from outlook_bridge.errors import ConfigError
from outlook_bridge.local_queue import IntentState
from outlook_bridge.testing import make_canary

from suv_deals.domain.canary import canary_subject, canary_target_hash

TARGET = "owner-test-mailbox@example.invalid"


def _harness(tmp_path: Path, **overrides: object) -> Harness:
    return Harness(tmp_path, config_overrides={"canary_target_address": TARGET, **overrides})


def _canary(h: Harness, **kwargs: object) -> object:
    values: dict[str, object] = {
        "mailbox_binding_id": MAILBOX_ID,
        "target_address": TARGET,
        "from_address": OWNER,
        "created_at": h.clock.now(),
    }
    values.update(kwargs)
    intent = make_canary(**values)  # type: ignore[arg-type]
    h.backend.add_canary(intent)
    return intent


def test_canary_is_claimed_sent_once_and_confirmed(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    canary = _canary(h)
    worker = h.worker()
    report = worker.start()
    assert report.sends["canary_submitted"] == 1, report.sends
    assert h.outlook.send_calls == [canary_subject(canary.canary_id)]  # type: ignore[attr-defined]
    assert h.backend.canary_claims == [canary.canary_id]  # type: ignore[attr-defined]
    item = h.outbox._items[0]
    assert [r.Address for r in item.Recipients._items] == [TARGET]
    assert [r["state"] for r in h.backend.canary_reports] == ["submitted_to_outbox"]
    # A later poll never sends it again, even while the backend still lists it.
    worker.tick(h.advance(timedelta(seconds=31)))
    assert len(h.outlook.send_calls) == 1
    # Sent Items evidence -> sent_items_confirmed.
    assert h.outlook.deliver_outbox(h.account) == 1
    worker.tick(h.advance(timedelta(seconds=31)))
    states = [r["state"] for r in h.backend.canary_reports]
    assert states == ["submitted_to_outbox", "sent_items_confirmed"]
    assert h.backend.canary_reports[-1]["observed_internet_message_id"] == canary.rfc_message_id  # type: ignore[attr-defined]
    h.close()


@pytest.mark.parametrize(
    ("config", "canary_kwargs", "code"),
    [
        ({"canary_target_address": None}, {}, "CANARY_TARGET_NOT_CONFIGURED"),
        ({"canary_target_address": "another-owner-box@example.invalid"}, {}, "CANARY_TARGET_MISMATCH"),
        ({}, {"mailbox_binding_id": OTHER_MAILBOX_ID}, None),
        ({}, {"from_address": "someone-else@example.invalid"}, None),
    ],
    ids=["no-target", "other-target", "other-mailbox", "other-sender"],
)
def test_local_refusals_never_send(
    tmp_path: Path, config: dict[str, object], canary_kwargs: dict[str, object], code: str | None
) -> None:
    h = _harness(tmp_path, **config)
    _canary(h, **canary_kwargs)
    worker = h.worker()
    if canary_kwargs.get("mailbox_binding_id") == OTHER_MAILBOX_ID:
        report = worker.start()  # the client refuses a canary for another mailbox outright
        assert report.sends["canary_fetch_failed"] == 1
        assert h.backend.canary_reports == []
    else:
        report = worker.start()
        assert report.sends["canary_refused"] == 1, report.sends
        [sent] = h.backend.canary_reports
        assert sent["state"] == "refused_before_send"
        if code is not None:
            assert sent["error_code"] == code
    assert h.outlook.send_calls == []
    assert h.backend.canary_claims == []
    h.close()


def test_the_sending_account_is_never_a_canary_target(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        parse_config(
            {
                "api_base_url": "https://api.example.invalid",
                "mailbox_binding_id": str(MAILBOX_ID),
                "worker_id": "desktop-test-1",
                "account_smtp_address": OWNER,
                "canary_target_address": OWNER.upper(),
            }
        )
    # A canary whose target hash is the sending account itself is refused locally too.
    h = _harness(tmp_path)
    intent = _canary(h, target_address=OWNER)
    h.config = h.config.model_copy(update={"canary_target_address": OWNER})
    worker = h.worker()
    worker.start()
    [sent] = h.backend.canary_reports
    assert sent["error_code"] == "CANARY_TARGET_IS_SENDER"
    assert intent.target_address_hash == canary_target_hash(OWNER)  # type: ignore[attr-defined]
    assert h.outlook.send_calls == []
    h.close()


def test_kill_switch_defers_and_expiry_refuses(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    _canary(h, ttl=timedelta(hours=1))
    h.backend.kill_switch = True
    worker = h.worker()
    report = worker.start()
    assert report.sends["canary_deferred_kill_switch"] == 1
    assert h.backend.canary_reports == [] and h.outlook.send_calls == []
    worker.tick(h.advance(timedelta(hours=2)))  # the canary's validity ended while paused
    [sent] = h.backend.canary_reports
    assert sent["state"] == "refused_before_send" and sent["refusal_reason"] == "intent_expired"
    assert h.outlook.send_calls == []
    h.close()


def test_interrupted_attempt_is_never_sent_again(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    canary = _canary(h)
    store = h.store
    store.record_canary_received(
        canary_id=canary.canary_id,  # type: ignore[attr-defined]
        payload_json=canary.model_dump_json(),  # type: ignore[attr-defined]
        message_id=canary.rfc_message_id,  # type: ignore[attr-defined]
        now=h.clock.now(),
    )
    assert store.begin_canary_attempt(canary.canary_id, h.clock.now())  # type: ignore[attr-defined]
    worker = h.worker()
    report = worker.start()  # "crash" after attempting: nothing in Outbox or Sent Items
    assert report.sends["canary_recovered"] == 1, report.sends
    assert h.outlook.send_calls == []
    [sent] = h.backend.canary_reports
    assert sent["state"] == "send_call_failed" and sent["error_code"] == "WORKER_INTERRUPTED"
    row = store.canary(canary.canary_id)  # type: ignore[attr-defined]
    assert row is not None and row.state == IntentState.SEND_FAILED
    h.close()


def test_only_replies_to_sent_canaries_are_reported(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    canary = _canary(h)
    worker = h.worker()
    worker.start()
    # A reply to a canary id this worker never sent and a personal message: ignored.
    stranger = make_canary(
        mailbox_binding_id=MAILBOX_ID, target_address=TARGET, from_address=OWNER, created_at=h.clock.now()
    )
    h.deliver_personal(in_reply_to=stranger.rfc_message_id, sender=TARGET)
    h.deliver_personal()
    # The owner's reply from the target mailbox.
    h.deliver_personal(
        subject="Re: activation test",
        sender=TARGET,
        in_reply_to=canary.rfc_message_id,  # type: ignore[attr-defined]
    )
    worker.tick(h.advance(timedelta(seconds=150)))
    worker.tick(h.advance(timedelta(seconds=31)))
    [reply] = h.backend.canary_replies
    assert reply["canary_id"] == str(canary.canary_id)  # type: ignore[attr-defined]
    assert reply["from_address_hash"] == canary_target_hash(TARGET)
    assert canary.rfc_message_id in reply["in_reply_to"]  # type: ignore[attr-defined]
    assert TARGET not in json.dumps(reply)  # headers and the sender's hash only
    assert h.backend.reply_posts == []  # never uploaded as a seller reply
    h.close()


def test_processor_is_absent_without_send_intents(tmp_path: Path) -> None:
    h = _harness(tmp_path, send_intents_enabled=False)
    _canary(h)
    worker = h.worker()
    worker.start()
    assert h.backend.canary_claims == [] and h.outlook.send_calls == []
    assert not any(path.startswith("/v1/mail-workers/canary-intents") for _m, path in h.backend.requests)
    assert CanaryProcessor is not None
    h.close()


def test_the_store_keeps_only_the_first_reply_per_canary(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    cid = uuid4()
    store = h.store
    store.record_canary_received(
        canary_id=cid, payload_json="{}", message_id=f"<canary-{cid}@example.invalid>", now=h.clock.now()
    )
    assert store.record_canary_reply(cid, '{"first": true}') is True
    assert store.record_canary_reply(cid, '{"second": true}') is False
    row = store.canary(cid)
    assert row is not None and row.reply_json == '{"first": true}'
    h.close()
