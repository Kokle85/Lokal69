"""``suv-deals capture-samples``: save a few reviewed pages of ONE source for parser work (owner-run).

Run it on the owner's own machine, one source and one search path at a time::

    SOURCE_NETWORK_ENABLED=true uv run suv-deals capture-samples \
        --source autoscout24_it --search-path /lst/toyota/rav-4 --yes

``SOURCE_NETWORK_ENABLED`` is set for this one command only; ``.env`` keeps it ``false``. The guard
rails are described in `suv_deals.crawling.sample_capture`. No database is used, no source flag or
gate changes, and nothing is published. Report the printed counts and statuses; the saved pages
stay on this machine (``<repo>/captures``, git-ignored).
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Final

import click

if TYPE_CHECKING:
    from suv_deals.crawling.sample_capture import CaptureOutcome

from suv_deals.cli_commands._common import (
    EXIT_OK,
    EXIT_PROBLEMS,
    EXIT_REFUSED,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    CliContext,
    echo,
    fail,
    load_settings,
    pass_cli,
    refuse,
    require_yes,
    run_async,
)

#: Exit code per stop reason: blocks and robots.txt refusals are "refused", a network failure is
#: "unavailable", anything else that ended the run is a plain problem.
_EXIT_FOR_STOP: Final[Mapping[str | None, int]] = {
    None: EXIT_OK,
    "access_blocked": EXIT_REFUSED,
    "robots_disallowed": EXIT_REFUSED,
    "robots_unreachable": EXIT_REFUSED,
    "network_error": EXIT_UNAVAILABLE,
}


@click.command("capture-samples")
@click.option(
    "--source",
    "source_key",
    required=True,
    metavar="KEY",
    help="Registered source key, for example autoscout24_it.",
)
@click.option(
    "--search-path",
    required=True,
    metavar="PATH",
    help="One reviewed search path without a query string, for example /lst/toyota/rav-4.",
)
@click.option(
    "--detail-limit",
    type=click.IntRange(0, 10),
    default=2,
    show_default=True,
    help="Detail pages to save (0 = search page only). Never fetched for card_only sources.",
)
@click.option(
    "--out-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output root (default: <repo>/captures, git-ignored).",
)
@click.option("--yes", is_flag=True, help="Confirm the requests to the source and the files written.")
@pass_cli
def capture_samples_command(
    cli: CliContext,
    *,
    source_key: str,
    search_path: str,
    detail_limit: int,
    out_dir: Path | None,
    yes: bool,
) -> None:
    """Save a few reviewed pages of one source to this machine for parser work (owner-run)."""
    settings = load_settings(cli)
    if not settings.source_network_enabled:
        refuse(
            "SOURCE_NETWORK_ENABLED=false blocks every real fetch; set SOURCE_NETWORK_ENABLED=true "
            "in the environment for this one command only"
        )
    require_yes(yes, "capture-samples")

    async def body() -> int | None:
        import anyio

        from suv_deals.clock import SystemClock
        from suv_deals.crawling.sample_capture import CaptureRefused, capture_samples, httpx_page_fetcher
        from suv_deals.domain.enums import TermsDecision
        from suv_deals.domain.sources import load_source_configs
        from suv_deals.settings import REPO_ROOT

        matches = [
            cfg
            for cfg in load_source_configs(settings.config_dir / "sources")
            if cfg.source_key == source_key
        ]
        if not matches:
            fail(f"unknown source {source_key!r} (see config/sources)", EXIT_USAGE)
        cfg = matches[0]
        if cfg.terms_decision in (TermsDecision.PENDING, TermsDecision.DO_NOT_USE):
            refuse(
                f"{source_key} has terms decision {cfg.terms_decision.value}; the owner's decision "
                "must be recorded in config first"
            )
        root = out_dir if out_dir is not None else REPO_ROOT / "captures"
        try:
            async with httpx_page_fetcher() as fetcher:
                outcome = await capture_samples(
                    cfg,
                    search_path=search_path,
                    detail_limit=detail_limit,
                    out_root=root,
                    fetcher=fetcher,
                    clock=SystemClock(),
                    sleep=anyio.sleep,
                )
        except CaptureRefused as exc:
            fail(str(exc), EXIT_PROBLEMS)
        _print_outcome(outcome)
        return _EXIT_FOR_STOP.get(outcome.stop_kind, EXIT_PROBLEMS)

    run_async(body)


def _status(status: int | None) -> str:
    return "no response" if status is None else f"HTTP {status}"


def _print_outcome(result: CaptureOutcome) -> None:
    echo(f"capture-samples: {result.source_key} ({result.host})")
    echo(
        f"  robots.txt : {_status(result.robots_status)}, {result.robots_availability}, "
        f"sha256 {(result.robots_sha256 or '-')[:12]}"
    )
    for step in result.steps:
        parts = [_status(step.status)]
        if step.status is not None:
            parts.append(f"{step.size} bytes")
        if step.truncated:
            parts.append("truncated at the size cap")
        if step.challenge is not None:
            parts.append(f"challenge: {step.challenge}")
        if step.error is not None:
            parts.append(f"error: {step.error}")
        echo(f"  {step.label:<10}: {', '.join(parts)}")
    if result.detail_links_matching:
        echo(f"  links      : {result.detail_links_matching} detail link(s) match the reviewed pattern")
    for note in result.skipped:
        echo(f"  skipped    : {note}")
    echo(f"  requests   : {result.requests}")
    if result.directory is not None:
        echo(f"  saved      : {result.directory.resolve()}")
    echo(f"  stopped    : {result.stop_reason or 'no (every requested page was saved)'}")
    echo("Share the counts and statuses above. The saved pages stay on this machine.")
