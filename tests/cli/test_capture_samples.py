"""``suv-deals capture-samples``: the refusals that come before any request or file (no network)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.cli.conftest import Cli, all_output

from suv_deals.adapters.base import FetchPurpose
from suv_deals.crawling.url_policy import PolicyDenied, SourceUrlPolicy
from suv_deals.domain.sources import load_source_configs

REPO = Path(__file__).resolve().parents[2]

NETWORK_ON = {"SOURCE_NETWORK_ENABLED": "true"}


def test_capture_is_refused_while_source_network_is_disabled(run_cli: Cli) -> None:
    result = run_cli(
        "capture-samples", "--source", "autoscout24_it", "--search-path", "/lst/toyota/rav-4", "--yes"
    )
    assert result.exit_code == 3
    assert "SOURCE_NETWORK_ENABLED=false" in all_output(result)


def test_capture_needs_yes_because_it_writes_files(run_cli: Cli) -> None:
    result = run_cli(
        "capture-samples",
        "--source",
        "autoscout24_it",
        "--search-path",
        "/lst/toyota/rav-4",
        env=NETWORK_ON,
    )
    assert result.exit_code == 3
    assert "--yes" in all_output(result)


def test_unknown_source_is_a_usage_error(run_cli: Cli) -> None:
    result = run_cli(
        "capture-samples", "--source", "no_such_source", "--search-path", "/lst/x/y", "--yes", env=NETWORK_ON
    )
    assert result.exit_code == 2
    assert "unknown source" in all_output(result)


def test_unreviewed_search_path_is_refused_before_any_request(run_cli: Cli) -> None:
    result = run_cli(
        "capture-samples",
        "--source",
        "autoscout24_it",
        "--search-path",
        "/ricerca/avanzata",
        "--yes",
        env=NETWORK_ON,
    )
    assert result.exit_code == 1
    assert "not in the reviewed allowed_search_paths" in all_output(result)


def test_detail_limit_above_the_cap_is_a_usage_error(run_cli: Cli) -> None:
    result = run_cli(
        "capture-samples",
        "--source",
        "autoscout24_it",
        "--search-path",
        "/lst/toyota/rav-4",
        "--detail-limit",
        "11",
        "--yes",
        env=NETWORK_ON,
    )
    assert result.exit_code == 2


def test_help_names_the_switch_and_the_guard(run_cli: Cli) -> None:
    result = run_cli("capture-samples", "--help")
    assert result.exit_code == 0
    assert "--search-path" in result.output
    assert "--detail-limit" in result.output


def test_crawler_route_needs_a_healthy_crawler_before_any_request(run_cli: Cli) -> None:
    # Nothing listens on port 9 of loopback: the health probe fails and nothing is fetched.
    result = run_cli(
        "capture-samples",
        "--source",
        "autoscout24_it",
        "--search-path",
        "/lst/toyota/rav-4",
        "--via",
        "crawl4ai",
        "--yes",
        env={**NETWORK_ON, "CRAWL4AI_BASE_URL": "http://127.0.0.1:9"},
    )
    assert result.exit_code == 4
    assert "not healthy" in all_output(result)
    assert "nothing was requested" in all_output(result)


def test_home_page_probe_is_reviewed_for_the_swiss_sources() -> None:
    configs = {c.source_key: c for c in load_source_configs(REPO / "config" / "sources")}
    for key in ("autoscout24_ch", "carforyou_ch", "tutti_ch"):
        cfg = configs[key]
        assert not cfg.enabled  # a reachability probe only; no crawl route
        assert list(cfg.allowed_search_paths) == ["^/$"]
        assert not cfg.allowed_detail_paths  # no detail page of these sites is reviewed
        SourceUrlPolicy.from_source(cfg).check(f"https://{cfg.allowed_hosts[0]}/", "search")


@pytest.mark.parametrize(
    ("key", "allowed", "refused"),
    [
        (
            "reklama5_mk",
            [
                ("/", "search"),
                ("/Search?q=Renault+Captur", "search"),
                ("/AdDetails?ad=1234567", "detail"),
            ],
            [("/Register", "search"), ("/Help/TermsOfService", "detail"), ("/AdDetails", "search")],
        ),
        (
            "pazar3_mk",
            [
                ("/", "search"),
                ("/oglasi/vozila/avtomobili", "search"),
                ("/oglasi/vozila/avtomobili/renault/captur", "search"),
                ("/oglas/vozila/avtomobili/renault/captur/prodazba/skopje/aerodrom/slug/1234567", "detail"),
            ],
            [
                ("/oglasi/zivealista", "search"),
                ("/oglas/zivealista/kukji-vili/prodazba/skopje/sopiste/slug/123", "detail"),
                ("/oglas/vozila/avtomobili/renault/captur/prodazba/skopje/aerodrom/slug/abc", "detail"),
            ],
        ),
    ],
)
def test_mk_sample_capture_paths_are_reviewed(
    key: str, allowed: list[tuple[str, FetchPurpose]], refused: list[tuple[str, FetchPurpose]]
) -> None:
    # 2026-10-16: search and ad paths read from the owner's captured home pages, for owner-run
    # sample captures; the sources stay disabled (no adapter) and store facts only.
    cfg = {c.source_key: c for c in load_source_configs(REPO / "config" / "sources")}[key]
    assert not cfg.enabled and cfg.adapter_version == "unimplemented"
    policy = SourceUrlPolicy.from_source(cfg)
    host = cfg.allowed_hosts[0]
    for path, purpose in allowed:
        policy.check(f"https://{host}{path}", purpose)
    for path, purpose in refused:
        with pytest.raises(PolicyDenied):
            policy.check(f"https://{host}{path}", purpose)
