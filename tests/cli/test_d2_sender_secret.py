"""OPS-09 (wave D2): the optional ``gmail_api`` route's credential can be stored with shipped tooling.

The runtime accepts ONLY a sealed grant referenced as ``secretbox:ops.email_sender_bindings/<id>``
(`workers.inquiry_handlers` refuses anything else: ``SECRET_REFERENCE_UNSUPPORTED``), but before D2
nothing outside tests called ``sender_bindings_repo.store_secret``; ``sender-binding create
--vault-ref`` only stored an external reference the runtime refuses. ``suv-deals sender-binding
store-secret`` seals the owner's OAuth grant (client secret and refresh token from the environment
for this one command, or hidden prompts; never echoed or logged) and prints the reference to set.

SYNTHETIC values only; nothing is contacted (no token exchange happens here).
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from click.testing import Result
from tests.cli.conftest import Cli

from suv_deals.domain.actor import ActorContext
from suv_deals.integrations.secret_box import SecretBox
from suv_deals.persistence import sender_bindings_repo
from suv_deals.persistence.database import Database
from suv_deals.persistence.transactions import unit_of_work

pytestmark = pytest.mark.db

KEY = "RkFLRUZBS0VGQUtFRkFLRUZBS0VGQUtFRkFLRUZBS0U="
CLIENT_SECRET = "SYNTHETIC-client-secret-0001"
REFRESH_TOKEN = "SYNTHETIC-refresh-token-0001"


def _create(run_cli: Cli, env: dict[str, str], workspace: UUID, provider: str) -> UUID:
    created = run_cli(
        "sender-binding",
        "create",
        "--workspace",
        str(workspace),
        "--provider",
        provider,
        "--account-id",
        f"synthetic-{provider}-account",
        "--from-address",
        f"owner-{provider.replace('_', '-')}@synthetic-mail.example",
        "--display-name",
        "Synthetic Sender",
        "--reason",
        "owner-authorized sending identity (synthetic)",
        "--yes",
        env=env,
    )
    assert created.exit_code == 0, created.output
    line = next(x for x in created.output.splitlines() if x.startswith("Created sender binding "))
    return UUID(line.split()[3])


def _store(run_cli: Cli, env: dict[str, str], workspace: UUID, binding: UUID, *extra: str) -> Result:
    return run_cli(
        "sender-binding",
        "store-secret",
        str(binding),
        "--workspace",
        str(workspace),
        "--client-id",
        "synthetic-client.apps.example",
        "--expected-version",
        "1",
        "--reason",
        "owner completed the OAuth consent (synthetic)",
        *extra,
        env=env,
    )


def test_store_secret_seals_the_grant_the_runtime_reads(
    run_cli: Cli, db_env: dict[str, str], db_url: str, workspace: UUID
) -> None:
    env = {
        **db_env,
        "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": KEY,
        "SUV_OAUTH_CLIENT_SECRET": CLIENT_SECRET,
        "SUV_OAUTH_REFRESH_TOKEN": REFRESH_TOKEN,
    }
    binding = _create(run_cli, env, workspace, "gmail_api")
    refused = _store(run_cli, env, workspace, binding)  # --yes is required
    assert refused.exit_code == 3
    stored = _store(run_cli, env, workspace, binding, "--yes")
    output = stored.output
    assert stored.exit_code == 0, output
    assert sender_bindings_repo.secret_reference_for(binding) in output
    assert CLIENT_SECRET not in output and REFRESH_TOKEN not in output

    async def opened() -> sender_bindings_repo.OAuthRefreshGrant:
        db = Database(db_url, set_role="suv_backend", min_size=1, max_size=2)
        await db.open()
        try:
            actor = ActorContext.system(workspace, request_id="test-open-grant")
            async with unit_of_work(db, actor) as conn:
                record, grant = await sender_bindings_repo.open_sealed_grant(
                    conn, actor, binding, box=SecretBox.from_config_value(KEY)
                )
            assert record.version == 2 and record.has_secret_envelope
            return grant
        finally:
            await db.close()

    grant = asyncio.run(opened())
    assert grant.client_id == "synthetic-client.apps.example"
    assert grant.client_secret.get_secret_value() == CLIENT_SECRET
    assert grant.refresh_token.get_secret_value() == REFRESH_TOKEN
    # A stale expected version changes nothing.
    stale = _store(run_cli, env, workspace, binding, "--yes")
    assert stale.exit_code != 0


def test_store_secret_refusals_change_nothing(run_cli: Cli, db_env: dict[str, str], workspace: UUID) -> None:
    secrets = {"SUV_OAUTH_CLIENT_SECRET": CLIENT_SECRET, "SUV_OAUTH_REFRESH_TOKEN": REFRESH_TOKEN}
    gmail = _create(
        run_cli, {**db_env, "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": KEY}, workspace, "gmail_api"
    )
    # No server-side secret box: refused before anything is sealed.
    unboxed = _store(run_cli, {**db_env, **secrets}, workspace, gmail, "--yes")
    assert unboxed.exit_code != 0 and "SECRET_BOX_NOT_CONFIGURED" in unboxed.output
    # No secret given and no terminal for a hidden prompt: a usage error.
    missing = _store(
        run_cli, {**db_env, "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": KEY}, workspace, gmail, "--yes"
    )
    assert missing.exit_code == 2
    assert "SUV_OAUTH_REFRESH_TOKEN" in missing.output or "SUV_OAUTH_CLIENT_SECRET" in missing.output
    # A secret with whitespace is refused without echoing it.
    bad = {**secrets, "SUV_OAUTH_REFRESH_TOKEN": "has whitespace SYNTHETIC"}
    invalid = _store(
        run_cli,
        {**db_env, **bad, "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": KEY},
        workspace,
        gmail,
        "--yes",
    )
    assert invalid.exit_code == 2 and "has whitespace" not in invalid.output
    # outlook_local stores no credential on the server.
    outlook = _create(run_cli, db_env, workspace, "outlook_local")
    local = _store(
        run_cli,
        {**db_env, **secrets, "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": KEY},
        workspace,
        outlook,
        "--yes",
    )
    assert local.exit_code != 0
    assert CLIENT_SECRET not in local.output
