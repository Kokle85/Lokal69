"""``suv-deals capture-samples``: save a few reviewed pages of ONE source for parser work (owner-run).

Run it on the owner's own machine, one source and one search path at a time::

    SOURCE_NETWORK_ENABLED=true uv run suv-deals capture-samples \
        --source autoscout24_it --search-path /lst/toyota/rav-4 --yes

``SOURCE_NETWORK_ENABLED`` is set for this one command only; ``.env`` keeps it ``false``. The guard
rails are described in `suv_deals.crawling.sample_capture`. No database is used, no source flag or
gate changes, and nothing is published. Report the printed counts and statuses; the saved pages
stay on this machine (``<repo>/captures``, git-ignored).

``--via crawl4ai`` loads the pages through the self-hosted Crawl4AI browser instead (headless,
honest User-Agent, no stealth; robots.txt is still read directly). It answers whether a page that
needs JavaScript, or a site that refuses plain HTTP clients, works through the crawler the worker
uses. Host processes reach the crawler at ``CRAWL4AI_BASE_URL=http://127.0.0.1:11235``.
"""

# ruff: noqa: PLC0415 - application modules are imported lazily so `--help` stays fast

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AsyncExitStack
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
    "--query",
    default=None,
    metavar="K=V&...",
    help="Optional query for the search page, for example priceto=3000; checked against robots.txt.",
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
@click.option(
    "--via",
    type=click.Choice(["httpx", "crawl4ai"]),
    default="httpx",
    show_default=True,
    help="Plain HTTPS client, or the self-hosted Crawl4AI browser (no stealth) for the pages.",
)
@click.option("--yes", is_flag=True, help="Confirm the requests to the source and the files written.")
@pass_cli
def capture_samples_command(
    cli: CliContext,
    *,
    source_key: str,
    search_path: str,
    query: str | None,
    detail_limit: int,
    out_dir: Path | None,
    via: str,
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
        from suv_deals.crawling.sample_capture import (
            CaptureRefused,
            capture_samples,
            crawl4ai_page_fetcher,
            httpx_page_fetcher,
        )
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
        if via == "crawl4ai":
            root = root / "via-crawl4ai"
        try:
            async with AsyncExitStack() as stack:
                fetcher = await stack.enter_async_context(httpx_page_fetcher())
                if via == "crawl4ai":
                    from suv_deals.adapters.crawl4ai_client import Crawl4AIClient

                    client = await stack.enter_async_context(Crawl4AIClient.from_settings(settings))
                    health = await client.health()
                    if not health.ok:
                        fail(
                            "the Crawl4AI crawler is not healthy at the configured CRAWL4AI_BASE_URL "
                            "(start it: docker compose up -d crawl4ai; host processes use "
                            "http://127.0.0.1:11235); nothing was requested from the source",
                            EXIT_UNAVAILABLE,
                        )
                    fetcher = await stack.enter_async_context(
                        crawl4ai_page_fetcher(client, source_key=cfg.source_key, robots_fetcher=fetcher)
                    )
                outcome = await capture_samples(
                    cfg,
                    search_path=search_path,
                    query=query,
                    detail_limit=detail_limit,
                    out_root=root,
                    fetcher=fetcher,
                    clock=SystemClock(),
                    sleep=anyio.sleep,
                )
        except CaptureRefused as exc:
            fail(str(exc), EXIT_PROBLEMS)
        _print_outcome(outcome, via=via)
        return _EXIT_FOR_STOP.get(outcome.stop_kind, EXIT_PROBLEMS)

    run_async(body)


def _whole(value: object) -> str:
    return "-" if value is None else f"{float(str(value)):,.0f}"


def _status(status: int | None) -> str:
    return "no response" if status is None else f"HTTP {status}"


def _print_outcome(result: CaptureOutcome, *, via: str = "httpx") -> None:
    echo(f"capture-samples: {result.source_key} ({result.host}) via {via}")
    echo(
        f"  robots.txt : {_status(result.robots_status)}, {result.robots_availability}, "
        f"sha256 {(result.robots_sha256 or '-')[:12]}"
    )
    for step in result.steps:
        parts = [_status(step.status)]
        if step.status is not None:
            parts.append(f"{step.size} bytes")
        if step.location is not None:
            parts.append(f"redirect to {step.location}")
        if step.truncated:
            parts.append("truncated at the size cap")
        if step.challenge is not None:
            parts.append(f"challenge: {step.challenge}")
        if step.error is not None:
            parts.append(f"error: {step.error}")
        echo(f"  {step.label:<10}: {', '.join(parts)}")
    if result.links_total is not None:
        echo(
            f"  links      : {result.links_total} on the search page, "
            f"{result.jsonld_links_total} in JSON-LD, "
            f"{result.detail_links_matching} match the reviewed detail pattern"
        )
        stats = result.search_offer_stats
        if stats.get("offers_with_price"):
            currency = stats["currency"] or ""
            echo(
                f"  offers     : {stats['offers_with_price']} with a price, "
                f"{_whole(stats['price_min'])}-{_whole(stats['price_max'])} {currency}".rstrip()
            )
        if stats.get("mileages_km"):
            echo(
                f"  mileage    : {stats['mileages_km']} with km, "
                f"{_whole(stats['mileage_km_min'])}-{_whole(stats['mileage_km_max'])} km"
            )
        for label, key in (("models", "models"), ("brands", "brands")):
            names = stats.get(key) or {}
            if names:
                listed = ", ".join(f"{name} x{count}" for name, count in list(names.items())[:10])
                echo(f"  {label:<11}: {listed}")
        for shape, count in result.detail_misses.items():
            echo(f"  detail miss: {count} x {shape}")
        if result.search_query_keys:
            keys = ", ".join(
                f"{name} x{count}" for name, count in list(result.search_query_keys.items())[:12]
            )
            echo(f"  query keys : {keys}")
        echo(f"  next link  : {result.search_rel_next} rel=next link(s)")
        for shape, count in list(result.search_link_shapes.items())[:5]:
            echo(f"  link forms : {count} x {shape}")
        for shape, count in list(result.search_path_shapes.items())[:6]:
            echo(f"  path forms : {count} x {shape} (anywhere in the page)")
        url_keys = [(key, count) for key, count in result.search_jsonld_shape.items() if ": url: " in key]
        for key, count in url_keys[:6]:
            echo(f"  json urls  : {count} x {key}")
        if result.search_jsonld_shape:
            echo(
                f"  json keys  : {len(result.search_jsonld_shape)} distinct key/kind pairs in structure.json"
            )
        if result.search_json_types:
            types = ", ".join(
                f"{name} x{count}" for name, count in list(result.search_json_types.items())[:6]
            )
            echo(f"  json types : {types}")
        for script in sorted(result.search_scripts, key=lambda item: -int(item["chars"]))[:5]:
            kind = script["type"] or "no type"
            ident = f", id={script['id']}" if script["id"] else ""
            origin = " (external)" if script["external"] else ""
            echo(f"  script     : {script['chars']} chars, {kind}{ident}{origin}")
    for label, detail_types in result.detail_json_types.items():
        listed = ", ".join(f"{name} x{count}" for name, count in list(detail_types.items())[:6]) or "none"
        echo(f"  {label} json types: {listed}")
    for note in result.skipped:
        echo(f"  skipped    : {note}")
    echo(f"  requests   : {result.requests}")
    if result.directory is not None:
        echo(f"  saved      : {result.directory.resolve()}")
    echo(f"  stopped    : {result.stop_reason or 'no (every requested page was saved)'}")
    echo("Share the counts and statuses above. The saved pages stay on this machine.")
