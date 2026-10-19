"""Wave D2 operations findings (process topology, environment template, doctor, crawler isolation).

- OPS-01: every job type the default handler registry knows is claimed by a documented process
  (the CLI default, both compose worker commands and the runbook process table), so seller
  inquiries are planned/sent/reconciled and seller replies are processed under the documented
  deployment;
- SEC-3 / OPS-07: the crawler networks have fixed bridge names, keep IPv6 off, and the runbook's
  host firewall covers the INPUT chain (traffic to the host's own addresses never passes
  DOCKER-USER) for both bridges, with an in-container verification step in the crawling gate;
- OPS-05: ``doctor --process dispatcher`` requires the Slack bot token, signing secret, channel
  and destination approval whenever seller-reply signals go to Slack (the owner's topology: native
  MCP Events for candidates, the private Slack signal for seller replies), not only when Slack is
  the candidate route.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.cli.conftest import Cli

from suv_deals.cli_commands.processes import DEFAULT_QUEUES, parse_queues
from suv_deals.workers.handlers import default_registry

REPO = Path(__file__).resolve().parents[2]


def _compose(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((REPO / name).read_text(encoding="utf-8"))
    return data


def _registered() -> set[str]:
    return {t.value for t in default_registry().job_types()}


# --------------------------------------------------------------------------------------------
# OPS-01: worker queue coverage
# --------------------------------------------------------------------------------------------


def test_default_worker_queues_cover_every_registered_job_type() -> None:
    assert {t.value for t in parse_queues(DEFAULT_QUEUES)} == _registered()
    assert {"seller_inquiry_plan", "seller_inquiry_send", "seller_inquiry_reconcile"} <= _registered()
    assert "seller_reply_process" in _registered()


@pytest.mark.parametrize("name", ["compose.yaml", "compose.production.yaml"])
def test_compose_worker_commands_cover_every_registered_job_type(name: str) -> None:
    services: dict[str, Any] = _compose(name)["services"]
    claimed: set[str] = set()
    for service in services.values():
        command = service.get("command")
        if not isinstance(command, list) or command[:2] != ["suv-deals", "worker"]:
            continue
        if "--queues" in command:
            claimed |= set(command[command.index("--queues") + 1].split(","))
        else:
            claimed |= {t.value for t in parse_queues(DEFAULT_QUEUES)}
    assert _registered() <= claimed, sorted(_registered() - claimed)


def test_makefile_dev_worker_uses_the_full_default() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    dev_workers = re.findall(r"\$\(CLI\) worker([^&\n]*)", makefile)
    assert dev_workers, "make dev must start a worker"
    assert all("--queues" not in args for args in dev_workers)


def test_runbook_worker_command_covers_every_registered_job_type() -> None:
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    match = re.search(r"\| Worker \| `suv-deals worker --queues ([a-z_,]+)`", runbook)
    assert match is not None
    assert _registered() <= set(match.group(1).split(","))


# --------------------------------------------------------------------------------------------
# SEC-3 / OPS-07: crawler isolation from the host
# --------------------------------------------------------------------------------------------

BRIDGES = {"crawler": "br-suv-crawlint", "crawler_egress": "br-suv-crawl"}


@pytest.mark.parametrize("name", ["compose.yaml", "compose.production.yaml"])
def test_crawler_networks_have_fixed_bridge_names_and_no_ipv6(name: str) -> None:
    networks: dict[str, Any] = _compose(name)["networks"]
    for network, bridge in BRIDGES.items():
        config = networks[network] or {}
        assert config.get("driver_opts", {}).get("com.docker.network.bridge.name") == bridge, network
        assert len(bridge) <= 15  # Linux interface name limit
        assert config.get("enable_ipv6") is False, network
    assert networks["crawler"]["internal"] is True
    services = _compose(name)["services"]
    assert set(services["crawl4ai"]["networks"]) == {"crawler", "crawler_egress"}
    assert "extra_hosts" not in services["crawl4ai"]


def test_runbook_firewall_covers_the_input_chain_for_both_crawler_bridges() -> None:
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    section = runbook[runbook.index("### 3.2 Crawler egress isolation") : runbook.index("### 3.3")]
    for bridge in BRIDGES.values():
        assert re.search(rf"iptables -I INPUT -i {bridge} .*-j DROP", section), bridge
    assert re.search(r"iptables -I DOCKER-USER -i br-suv-crawl .*-j DROP", section)
    assert "br-<" not in section  # no placeholder bridge ids any more
    assert "ip6tables" in section and "enable_ipv6: false" in section
    assert "must FAIL" in section and "create_connection" in section
    gate = runbook[runbook.index("## 9. Activation checklist") : runbook.index("## 10.")]
    assert "INPUT" in gate and "3.2" in gate


def test_dev_compose_comment_does_not_claim_the_host_is_unreachable() -> None:
    text = (REPO / "compose.yaml").read_text(encoding="utf-8")
    assert "a missing name is no isolation" in text


# --------------------------------------------------------------------------------------------
# OPS-05: doctor and the Slack seller-reply route
# --------------------------------------------------------------------------------------------

#: The owner's chosen topology (synthetic values; nothing is contacted with --no-db).
MIXED_TOPOLOGY = {
    "DATABASE_URL": "postgresql://fakeuser:FAKE-pass-0001@db-fake.invalid:5432/fakedb",
    "DATABASE_SET_ROLE": "suv_backend",
    "APP_BASE_URL": "https://deals-fake.example.invalid",
    "ALLOW_EXTERNAL_NOTIFICATIONS": "true",
    "EVENT_BRIDGE_ENABLED": "true",
    "EVENT_BRIDGE_PROVIDER": "mcp_events",
    "MCP_EVENTS_ENABLED": "true",
    "NOTIFICATION_PROVIDER": "mcp_events",
    "SELLER_REPLY_SIGNAL_PROVIDER": "slack",
    "MCP_EVENT_SUBSCRIPTION_SECRET_ENCRYPTION_KEY": "RkFLRUZBS0VGQUtFRkFLRUZBS0VGQUtFRkFLRUZBS0U=",
}
SLACK_SETTINGS = (
    "SLACK_BOT_TOKEN",
    "SLACK_SIGNING_SECRET",
    "SLACK_CHANNEL_ID",
    "SLACK_DESTINATION_APPROVAL_REF",
)


def _dispatcher_findings(run_cli: Cli, env: dict[str, str]) -> dict[str, dict[str, Any]]:
    result = run_cli("doctor", "--no-db", "--json", "--process", "dispatcher", env=env)
    findings = json.loads(result.output)["findings"]
    return {f["detail"].split(":")[0]: f for f in findings if f["area"] == "config"}


def test_doctor_requires_slack_for_seller_reply_signals(run_cli: Cli) -> None:
    findings = _dispatcher_findings(run_cli, MIXED_TOPOLOGY)
    for name in SLACK_SETTINGS:
        assert name in findings, name
        assert findings[name]["status"] == "error", findings[name]
        assert "seller-reply" in findings[name]["detail"]


def test_doctor_does_not_require_slack_without_external_notifications(run_cli: Cli) -> None:
    findings = _dispatcher_findings(run_cli, {**MIXED_TOPOLOGY, "ALLOW_EXTERNAL_NOTIFICATIONS": "false"})
    assert not set(SLACK_SETTINGS) & set(findings)
    disabled = _dispatcher_findings(run_cli, {**MIXED_TOPOLOGY, "SELLER_REPLY_SIGNAL_PROVIDER": "disabled"})
    assert not set(SLACK_SETTINGS) & set(disabled)


def test_runbook_names_slack_for_seller_reply_signals() -> None:
    runbook = (REPO / "docs" / "runbook.md").read_text(encoding="utf-8")
    table = runbook[runbook.index("### 2.1 Environment variables per process") : runbook.index("## 3.")]
    row = next(line for line in table.splitlines() if line.startswith("| `SLACK_BOT_TOKEN`"))
    assert "SELLER_REPLY_SIGNAL_PROVIDER=slack" in row
