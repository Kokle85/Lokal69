"""``POST /mcp`` refuses a client that keeps presenting rejected tokens BEFORE any verification
(``api.middleware.PreAuthLimiter``; no database, no network: the pool is never opened)."""

from __future__ import annotations

from suv_deals.api.middleware import DEFAULT_PREAUTH_LIMIT
from suv_deals.domain.enums import Scope
from tests.mcp.conftest import SigningKeys, TokenFactory, make_settings, mcp_client, offline_db


async def test_failed_tokens_exhaust_the_client_budget_before_verification(
    keys: SigningKeys, tokens: TokenFactory
) -> None:
    async with mcp_client(make_settings(), offline_db(), keys) as client:
        forged = tokens.mint("synthetic-user", key=keys.stranger_private, scopes=[Scope.DEALS_READ])
        for _ in range(DEFAULT_PREAUTH_LIMIT.capacity):
            response = await client.rpc("tools/list", token=forged)
            assert response.status_code == 401
        valid = tokens.mint("synthetic-user", scopes=[Scope.DEALS_READ])
        limited = await client.rpc("tools/list", token=valid)
        assert limited.status_code == 429  # refused before the signature check
        assert int(limited.headers["retry-after"]) >= 1
        assert limited.json()["error"] == "rate_limited"
