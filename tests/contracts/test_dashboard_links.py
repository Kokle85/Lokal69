"""Contract: every dashboard link the backend builds opens a real dashboard route.

The backend builds the authenticated dashboard links carried by Slack signals, owner alerts and
MCP Events payloads (`domain.notifications.dashboard_case_url`,
`domain.replies.dashboard_reply_url`); the dashboard declares its routes in
``dashboard/src/App.tsx``. A link without a matching route lands on "Page not found", so the owner
never sees the case or reply the alert was about. This test reads the route table from the
dashboard source (no Node needed) and checks that each backend link path matches a route that is
not the catch-all. Synthetic ``example.invalid`` base URL only.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from suv_deals.domain.notifications import dashboard_case_url
from suv_deals.domain.replies import dashboard_reply_url

APP_TSX = Path(__file__).resolve().parents[2] / "dashboard" / "src" / "App.tsx"
BASE = "https://dashboard.example.invalid"

_ROUTE_PATH = re.compile(r"""\bpath:\s*['"]([^'"]+)['"]""")


def _route_patterns() -> list[re.Pattern[str]]:
    source = APP_TSX.read_text(encoding="utf-8")
    patterns: list[re.Pattern[str]] = []
    for raw in _ROUTE_PATH.findall(source):
        if raw in ("*", "/"):
            continue  # the catch-all "Page not found" route and the layout root
        segments = [s for s in raw.strip("/").split("/") if s]
        regex = "/".join("[^/]+" if s.startswith(":") else re.escape(s) for s in segments)
        patterns.append(re.compile(f"^/{regex}$"))
    return patterns


def test_route_table_is_readable() -> None:
    patterns = _route_patterns()
    # A guard against a silently broken parser: the dashboard has well over a dozen routes.
    assert len(patterns) >= 10
    assert any(p.match("/reviews/x") for p in patterns)


@pytest.mark.parametrize(
    "url",
    [
        dashboard_case_url(BASE, uuid.uuid4()),
        dashboard_reply_url(BASE, uuid.uuid4(), uuid.uuid4()),
        dashboard_case_url(BASE + "/", uuid.uuid4()),
        dashboard_reply_url(BASE + "/", uuid.uuid4(), uuid.uuid4()),
    ],
)
def test_backend_dashboard_links_match_a_dashboard_route(url: str) -> None:
    path = urlsplit(url).path
    assert any(p.match(path) for p in _route_patterns()), f"no dashboard route serves {path}"
