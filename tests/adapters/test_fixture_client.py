"""Offline FixtureCrawlClient behaviour (never touches the network)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from tests.adapters.conftest import FIXTURES, NOW

from suv_deals.adapters.base import CrawlClient
from suv_deals.adapters.fixture_client import FixtureCrawlClient, FixtureManifest, load_manifest
from suv_deals.clock import FrozenClock
from suv_deals.domain.enums import AccessState
from suv_deals.errors import ValidationFailed

DE = "https://dealer.example"


async def test_serves_fixture_files(client: FixtureCrawlClient) -> None:
    document = await client.fetch(f"{DE}/fahrzeug/TEST-204", purpose="detail", source_key="fixture_dealer_de")
    body = (FIXTURES / "fixture_dealer_de" / "detail_normal.html").read_bytes()
    assert document.html == body.decode("utf-8")
    assert document.raw_content_hash == hashlib.sha256(body).hexdigest()
    assert document.fetched_at == NOW
    fetch = document.fetch
    assert (fetch.http_status, fetch.success, fetch.access_state) == (200, True, AccessState.OK)
    assert fetch.bytes == len(body) and fetch.redirect_count == 0
    assert fetch.response_headers == {"content-type": "text/html; charset=utf-8"}
    assert fetch.crawler_version == "fixture-client@1.0.0"
    assert [(r.url, r.purpose, r.source_key) for r in client.requests] == [
        (f"{DE}/fahrzeug/TEST-204", "detail", "fixture_dealer_de")
    ]
    assert isinstance(client, CrawlClient)


async def test_rate_limit_with_retry_after_seconds_and_http_date(client: FixtureCrawlClient) -> None:
    seconds = await client.fetch(f"{DE}/suche?typ=viel", purpose="search", source_key="fixture_dealer_de")
    assert seconds.fetch.access_state == AccessState.RATE_LIMITED
    assert seconds.fetch.retry_after_seconds == 120
    dated = await client.fetch(f"{DE}/suche?typ=viel-datum", purpose="search", source_key="fixture_dealer_de")
    assert dated.fetch.retry_after_seconds == 300  # 10:05 GMT minus frozen 10:00 UTC


@pytest.mark.parametrize(("query", "code"), [("langsam", "TIMEOUT"), ("verbindung", "CONNECTION_ERROR")])
async def test_simulated_transport_failures(client: FixtureCrawlClient, query: str, code: str) -> None:
    document = await client.fetch(f"{DE}/suche?typ={query}", purpose="search", source_key="fixture_dealer_de")
    assert document.html is None
    assert document.fetch.http_status is None
    assert (document.fetch.success, document.fetch.access_state) == (False, AccessState.TRANSIENT_ERROR)
    assert document.fetch.error_code == code


async def test_redirect_to_other_host(client: FixtureCrawlClient) -> None:
    document = await client.fetch(
        f"{DE}/suche?typ=umleitung", purpose="search", source_key="fixture_dealer_de"
    )
    assert document.final_url == "https://elsewhere.example/suche?typ=suv"
    assert document.fetch.redirect_count == 1


@pytest.mark.parametrize(
    ("url", "source_key", "state", "code"),
    [
        (f"{DE}/unbekannt", "fixture_dealer_de", AccessState.NOT_FOUND, "FIXTURE_NOT_FOUND"),
        (f"{DE}/fahrzeug/TEST-204", "fixture_dealer_it", AccessState.NOT_FOUND, "FIXTURE_NOT_FOUND"),
        (f"{DE}/fahrzeug/TEST-204", "no_such_source", AccessState.NOT_FOUND, "FIXTURE_NOT_FOUND"),
        ("http://127.0.0.1/admin", "fixture_dealer_de", AccessState.POLICY_DENIED, "POLICY_DENIED"),
        (
            "http://169.254.169.254/latest/meta-data/",
            "fixture_dealer_de",
            AccessState.POLICY_DENIED,
            "POLICY_DENIED",
        ),
        ("file:///etc/passwd", "fixture_dealer_de", AccessState.POLICY_DENIED, "POLICY_DENIED"),
    ],
)
async def test_unknown_and_unsafe_urls(
    client: FixtureCrawlClient, url: str, source_key: str, state: AccessState, code: str
) -> None:
    document = await client.fetch(url, purpose="detail", source_key=source_key)
    assert document.html is None
    assert document.fetch.access_state == state
    assert document.fetch.error_code == code


def _write_manifest(directory: Path, routes: dict[str, object], files: dict[str, str] | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source_key": "tmp_source",
        "designation": "synthetic",
        "created": "2026-10-06",
        "parser_version": "schemaorg_dealer@1.0.0",
        "description": "temporary synthetic manifest",
        "files": files if files is not None else {"a.html": "file a"},
        "routes": routes,
    }
    (directory / "MANIFEST.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")


async def test_byte_cap(tmp_path: Path) -> None:
    (tmp_path / "a.html").write_text("<p>" + "x" * 20_000 + "</p>", encoding="utf-8")
    _write_manifest(tmp_path, {"https://tmp.example/a": {"file": "a.html", "description": "big"}})
    client = FixtureCrawlClient([tmp_path], clock=FrozenClock(NOW), max_response_bytes=10_000)
    document = await client.fetch("https://tmp.example/a", purpose="detail", source_key="tmp_source")
    assert document.fetch.error_code == "RESPONSE_TOO_LARGE"
    assert document.fetch.access_state == AccessState.UNEXPECTED_CONTENT


async def test_path_traversal_is_refused(tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
    inner = tmp_path / "inner"
    _write_manifest(
        inner,
        {"https://tmp.example/a": {"file": "../secret.txt", "description": "escape attempt"}},
        files={"../secret.txt": "escape"},
    )
    client = FixtureCrawlClient([inner], clock=FrozenClock(NOW))
    with pytest.raises(ValidationFailed):
        await client.fetch("https://tmp.example/a", purpose="detail", source_key="tmp_source")


@pytest.mark.parametrize(
    ("routes", "files"),
    [
        (
            {
                "https://tmp.example/a": {
                    "file": "a.html",
                    "headers": {"set-cookie": "x"},
                    "description": "bad",
                }
            },
            None,
        ),
        ({"https://tmp.example/a": {"file": "b.html", "description": "undescribed file"}}, None),
        ({"https://tmp.example/a": {"description": "success without file"}}, None),
        ({"https://tmp.example/a": {"file": "a.html", "description": "x", "simulate": "explode"}}, None),
    ],
)
def test_invalid_manifests_are_rejected(tmp_path: Path, routes: dict[str, object], files: object) -> None:
    _write_manifest(tmp_path, routes)
    with pytest.raises(ValidationFailed):
        load_manifest(tmp_path)


def test_missing_manifest_and_duplicate_sources(tmp_path: Path) -> None:
    with pytest.raises(ValidationFailed):
        FixtureCrawlClient([tmp_path])
    with pytest.raises(ValidationFailed):
        FixtureCrawlClient([FIXTURES / "fixture_dealer_de", FIXTURES / "fixture_dealer_de"])


def test_manifest_model_is_strict() -> None:
    with pytest.raises(ValueError, match="designation"):
        FixtureManifest.model_validate(
            {
                "source_key": "x_source",
                "designation": "maybe",
                "created": "2026-10-06",
                "parser_version": "v",
                "description": "desc",
                "files": {},
                "routes": {},
            }
        )
