"""Dashboard import-cost calculator route (``GET /api/import-costs/estimate``; Supabase user JWT).

North Macedonian customs duty, motor-vehicle tax (ДМВ) and import VAT for one vehicle, plus the
landed cost with the owner's fixed per-vehicle transport, through the SAME read as dot's MCP tool
``deals_estimate_import_costs`` and the CLI (``queries.get_import_cost_estimate``). Same handler
shape as ``api.inquiry_routes``: authenticate, check ``deals:read``, parse the closed query model,
run ONE short workspace-scoped transaction and answer with the shared ``ResponseEnvelope``.

- Input: ``ImportCostEstimateQuery`` (the tool input as flat query parameters) is converted to
  ``DealsEstimateImportCostsInput`` and then to the service request exactly as the MCP handler
  does (``mcp.tools.import_cost_request``), so both surfaces apply the same rules. ``listing_id``
  is optional; every vehicle parameter that is given replaces the listing's fact.
- Read-only: nothing is stored (no valuation, cost line, alert, rate or audit row), so it is a GET
  with no idempotency key. The draft rule files and the cost profile come from
  ``Settings.config_dir``. Without an ACTIVE, owner-approved rule set the result is an
  ``indicative_preview`` on unapproved rules (``production_ready: false``, the MK/EN ``banner``,
  totals named ``indicative_total``) or ``unavailable``; the envelope warnings reuse the existing
  codes (``TAX_RULES_UNAPPROVED``, ``UNKNOWN_COSTS``, ``FIXTURE_DATA``, ``SELLER_CLAIMS_UNVERIFIED``).
- A missing or foreign-workspace ``listing_id`` is the same ``404 NOT_FOUND``.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Request
from starlette.responses import Response

from suv_deals.api.deps import Authenticated, api_state, in_transaction, query_model, require_scope
from suv_deals.api.errors import JSON_MEDIA_TYPE
from suv_deals.api.schemas import V11_ROUTE_INDEX, ImportCostEstimateQuery
from suv_deals.mcp.tools import import_cost_request
from suv_deals.persistence import queries
from suv_deals.views.common import ResponseEnvelope

router = APIRouter()

ESTIMATE: Final = V11_ROUTE_INDEX["GET /api/import-costs/estimate"]


@router.get("/api/import-costs/estimate")
async def estimate_import_costs(request: Request, auth: Authenticated) -> Response:
    require_scope(request, auth, ESTIMATE.scope)
    query = query_model(request, ImportCostEstimateQuery)
    service_request = import_cost_request(query.to_tool_input())
    state = api_state(request)
    config_dir = state.settings.config_dir
    result = await in_transaction(
        state,
        auth.actor,
        lambda conn: queries.get_import_cost_estimate(
            conn, auth.actor, service_request, config_dir=config_dir
        ),
    )
    body: ResponseEnvelope[Any] = result.envelope(auth.request_id)
    return Response(
        content=body.model_dump_json(), status_code=ESTIMATE.success_status, media_type=JSON_MEDIA_TYPE
    )


__all__ = ["ESTIMATE", "router"]
