"""SEC-1 (wave D2): the claim's ``worker_id`` names this local store, not just the configuration.

The backend now grants a running send intent to ONE ``worker_id`` (a claim by any other id is
refused ``ALREADY_CLAIMED``). The desktop's "never attempt an intent twice" guard lives in its
SQLite store, which is per data_dir, so the claim identity must change whenever that store is
new: a reinstall, a wiped store or a second data_dir with the same credential claims as a
DIFFERENT worker and is refused by the server instead of calling ``.Send`` a second time.

The store instance id is random, written once when the store is created, and starts with a
letter so the backend's audit redaction (phone-number rules on digit runs) never rewrites it.
Everything is synthetic; nothing is sent.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from bridge_support import MAILBOX_ID, OWNER, TOKEN, Harness, make_config
from outlook_bridge import cli
from outlook_bridge.api_client import BridgeApiClient, ClientIdentity, claim_worker_id
from outlook_bridge.credentials import CredentialManager
from outlook_bridge.local_queue import LocalStore
from outlook_bridge.testing import FakeBackend, make_intent
from outlook_bridge.wire import WorkerSendReport

_INSTANCE = re.compile(r"^s[0-9a-f]{16}$")
_WIRE_WORKER_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _store_path(tmp_path: Path, name: str = "data") -> Path:
    return tmp_path / name / "bridge-state.sqlite3"


def test_store_instance_id_is_created_once_and_survives_reopen(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = LocalStore.open(path, MAILBOX_ID)
    first = store.store_instance_id
    store.close()
    assert _INSTANCE.fullmatch(first)
    reopened = LocalStore.open(path, MAILBOX_ID)
    assert reopened.store_instance_id == first
    reopened.close()


def test_a_wiped_store_or_a_second_data_dir_is_a_new_instance(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    store = LocalStore.open(path, MAILBOX_ID)
    original = store.store_instance_id
    store.close()
    second = LocalStore.open(_store_path(tmp_path, "other-data"), MAILBOX_ID)
    assert second.store_instance_id != original
    second.close()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    wiped = LocalStore.open(path, MAILBOX_ID)
    assert wiped.store_instance_id != original
    wiped.close()
    one, two = LocalStore.in_memory(MAILBOX_ID), LocalStore.in_memory(MAILBOX_ID)
    assert one.store_instance_id != two.store_instance_id


def test_a_store_from_before_d2_gets_an_instance_id_once(tmp_path: Path) -> None:
    path = _store_path(tmp_path)
    LocalStore.open(path, MAILBOX_ID).close()
    raw = sqlite3.connect(str(path))
    raw.execute("delete from meta where key = 'store_instance_id'")
    raw.commit()
    raw.close()
    upgraded = LocalStore.open(path, MAILBOX_ID)
    added = upgraded.store_instance_id
    upgraded.close()
    assert _INSTANCE.fullmatch(added)
    again = LocalStore.open(path, MAILBOX_ID)
    assert again.store_instance_id == added
    again.close()


def test_claim_worker_id_combines_config_and_store_within_the_wire_limits() -> None:
    assert claim_worker_id("desktop-1", "s0123456789abcdef") == "desktop-1.s0123456789abcdef"
    long_id = "w" * 128
    combined = claim_worker_id(long_id, "s0123456789abcdef")
    assert len(combined) == 128 and combined.endswith(".s0123456789abcdef")
    assert _WIRE_WORKER_ID.fullmatch(combined)


def test_claim_sends_the_store_scoped_worker_id(tmp_path: Path) -> None:
    store = LocalStore.open(_store_path(tmp_path), MAILBOX_ID)
    seen: list[httpx.Request] = []
    intent_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        answer = {"schema_version": "1.0", "intent_id": str(intent_id), "proceed": True}
        return httpx.Response(200, json=answer)

    client = BridgeApiClient(
        "https://api.example.invalid",
        token_provider=lambda: TOKEN,
        identity=ClientIdentity(MAILBOX_ID, "desktop-test-1", store_instance_id=store.store_instance_id),
        transport=httpx.MockTransport(handler),
    )
    client.claim_send_intent(intent_id)
    body = json.loads(seen[0].content)
    assert body["worker_id"] == f"desktop-test-1.{store.store_instance_id}"
    store.close()


def test_cli_identity_uses_the_opened_store(tmp_path: Path) -> None:
    config = make_config(tmp_path / "data")
    store = LocalStore.open(config.store_path(), config.mailbox_binding_id)
    identity = cli.client_identity(config, store)
    assert identity.mailbox_binding_id == config.mailbox_binding_id
    assert identity.claim_worker_id == f"{config.worker_id}.{store.store_instance_id}"
    store.close()


def test_two_installations_claim_as_different_workers(tmp_path: Path) -> None:
    """The fake backend records the claim worker ids: two harnesses (two stores) differ."""
    first = Harness(tmp_path / "pc-a")
    second = Harness(tmp_path / "pc-b")
    assert first.config.worker_id == second.config.worker_id  # the same configuration
    assert first.api_identity.claim_worker_id != second.api_identity.claim_worker_id
    # Reports still carry the configured worker id (health and audit continuity).
    report_fields = WorkerSendReport.model_fields
    assert "worker_id" in report_fields


def test_a_wiped_store_never_sends_the_same_intent_again(tmp_path: Path) -> None:
    """The submission report is lost (backend outage after ``.Send``) and the local store is
    wiped: the backend still lists the intent, the new store claims it as another worker, is
    refused ``ALREADY_CLAIMED`` and never calls ``.Send`` a second time."""
    h = Harness(tmp_path)
    intent = make_intent(
        inquiry_id=uuid4(),
        mailbox_binding_id=MAILBOX_ID,
        from_address=OWNER,
        created_at=h.clock.now() - timedelta(minutes=1),
    )
    h.backend.add_intent(intent)
    report_path = f"/v1/mail-workers/send-intents/{intent.intent_id}/report"
    h.backend.fail(report_path, *[FakeBackend.raise_(httpx.ConnectError) for _ in range(10)])
    worker = h.worker()
    assert worker.start().sends["submitted"] == 1
    assert h.outlook.send_calls == [intent.subject]
    assert intent.intent_id in h.backend.open_intents  # the report never arrived
    worker.shutdown()
    # The local store is wiped (or a second installation with the same credential starts).
    h.store.close()
    h.store = LocalStore.in_memory(MAILBOX_ID)
    h.credentials = CredentialManager(h.cred_store, h.store)
    h.api_identity = ClientIdentity(MAILBOX_ID, h.config.worker_id, h.store.store_instance_id)
    h.api = BridgeApiClient(
        h.config.api_base_url,
        token_provider=lambda: h.credentials.token(h.clock.now()),
        identity=h.api_identity,
        transport=h.backend.transport(),
    )
    h.backend.faults.clear()
    fresh = h.worker()
    fresh.start()
    assert h.backend.claims == [intent.intent_id, intent.intent_id]
    assert h.backend.claim_details[-1] == "ALREADY_CLAIMED"
    assert h.outlook.send_calls == [intent.subject]  # never a second message
    refused = [r for r in h.backend.reports if r["intent_id"] == str(intent.intent_id)]
    assert refused and refused[-1]["state"] == "refused_before_send"
    assert refused[-1]["refusal_reason"] == "intent_invalid"
    h.close()
