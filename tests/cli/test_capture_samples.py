"""``suv-deals capture-samples``: the refusals that come before any request or file (no network)."""

from __future__ import annotations

from tests.cli.conftest import Cli, all_output

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
