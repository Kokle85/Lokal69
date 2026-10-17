"""F5 / F9 / F10 / F2 (wave D2): the spec 34 handoff and status documents exist and stay honest.

Before D2 the README pointed at an ``IMPLEMENTATION_STATUS.md`` that did not exist, and
``ACTIVATION_GATES.md``, ``SECURITY.md``, ``CHANGELOG.md`` and ``docs/acceptance_matrix.md`` (spec
6 layout, spec 33 M8 exit, spec 34) were missing; no document marked U1-U12 (spec 37.10) or
answered the spec 34 final-status questions. These checks pin the structure, not the prose:

- every file the README links exists;
- U1-U12 each carry a spec 32 state; the nine spec 34 questions are answered;
- every seeded activation gate (``persistence.gates.SPEC_GATES``) is listed with its status;
- every spec 31 test-matrix row is in the acceptance matrix as passed / failed / blocked / not run,
  and every test path it cites exists;
- F9: the source register's live smoke is the gated ``crawl once`` procedure (no ``live`` pytest
  marker exists); F2: the consequence of missing MK evidence is stated; F10: vehicle documents
  stay in the mailbox (manual CoC/CO2 verification) in the activation doc and the runbook.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest

from suv_deals.cli import cli
from suv_deals.persistence.gates import SPEC_GATES

REPO = Path(__file__).resolve().parents[2]
SPEC = REPO / "docs" / "spec" / "suv-deal-system-build-spec.md"
STATES = (
    "implemented",
    "fixture_verified",
    "integration_verified",
    "live_verified",
    "active",
    "blocked",
    "not_requested",
)
MATRIX_STATES = ("passed", "failed", "blocked", "not run")
SPEC34_QUESTIONS = (
    "What runs now?",
    "Where does it run?",
    "Which sources are truly working?",
    "When was each last checked?",
    "Which calculations are evidence-supported?",
    "Can dot read the queue?",
    "Can dot actually be triggered?",
    "Were notifications accepted by the intended destination?",
    "What remains blocked?",
)


def _text(relative: str) -> str:
    return (REPO / relative).read_text(encoding="utf-8")


def test_readme_links_resolve() -> None:
    readme = _text("README.md")
    links = re.findall(r"\]\(([^)#\s]+)\)", readme)
    for required in (
        "IMPLEMENTATION_STATUS.md",
        "ACTIVATION_GATES.md",
        "SECURITY.md",
        "CHANGELOG.md",
        "docs/acceptance_matrix.md",
    ):
        assert required in links, required
    for link in links:
        if "://" not in link:
            assert (REPO / link).exists(), link


def test_status_marks_u1_to_u12_and_answers_the_spec_34_questions() -> None:
    status = _text("IMPLEMENTATION_STATUS.md")
    for number in range(1, 13):
        row = re.search(rf"^\| U{number} \|(.*)$", status, re.M)
        assert row is not None, f"U{number}"
        assert any(f"`{state}`" in row.group(1) for state in STATES), row.group(0)
    for question in SPEC34_QUESTIONS:
        assert question in status, question


def test_activation_gates_lists_every_seeded_gate() -> None:
    gates = _text("ACTIVATION_GATES.md")
    for gate in SPEC_GATES:
        row = re.search(rf"^\| `{gate.capability}` \|(.*)$", gates, re.M)
        assert row is not None, gate.capability
        assert f"`{gate.status.value}`" in row.group(1), (gate.capability, gate.status.value)


def _spec31_areas() -> list[str]:
    spec = SPEC.read_text(encoding="utf-8")
    section = spec[spec.index("## 31 Test matrix") : spec.index("### Required fixture coverage")]
    rows = [line for line in section.splitlines() if line.startswith("| ") and not line.startswith("| Area")]
    return [row.split("|")[1].strip() for row in rows if not set(row) <= {"|", "-", " "}]


def test_acceptance_matrix_covers_every_spec_31_row() -> None:
    matrix = _text("docs/acceptance_matrix.md")
    areas = _spec31_areas()
    assert len(areas) == 26
    for area in areas:
        row = re.search(rf"^\| {re.escape(area)} \|(.*)$", matrix, re.M)
        assert row is not None, area
        assert any(f"**{state}**" in row.group(1) for state in MATRIX_STATES), row.group(0)
    for path in re.findall(r"`((?:tests|dashboard|desktop)/[^`*]+?)`", matrix):
        assert (REPO / path).exists(), path


def test_source_register_live_smoke_is_the_gated_crawl_once_procedure() -> None:
    register = _text("docs/source_access_register.md")
    step = register[register.index("6. **Live low-volume smoke.**") : register.index("7. **Enable.**")]
    assert "suv-deals crawl once --source" in step and "--max-pages 1" in step
    assert "--max-jobs N" in step and "at most `max_detail_jobs_per_run`" not in step
    assert "suv-deals sources smoke-evidence <key>" in step
    assert "pytest marker" not in step
    assert "insufficient_comparables" in register  # F2: no MK evidence -> no valuation


def _source_rows(key: str) -> list[str]:
    """The register row and the ACTIVATION_GATES row of one source."""
    register = _text("docs/source_access_register.md").splitlines()
    gates = _text("ACTIVATION_GATES.md").splitlines()
    return [
        next(line for line in register if line.startswith(f"| `{key}` |")),
        next(line for line in gates if line.startswith(f"| `{key}` |")),
    ]


def test_autoscout24_de_rows_record_step_5_and_name_the_live_smoke_next() -> None:
    """Step 5 (saved spec-31 fixtures) is done on SYNTHETIC pages, so ``autoscout24_de`` is
    ``fixture_tested``; its rows must say so, never claim a live verification, and name the owner's
    live smoke (step 6) as the next step (earlier regression: the live smoke was named while
    ``crawl once`` still refused the ``untested`` sources)."""
    for row in _source_rows("autoscout24_de"):
        # The smoke never confirms the .de filters (nothing records pageQuery).
        assert "to confirm the unverified model slugs" not in row
        assert "to confirm the search filters" not in row
        assert "fixture_tested" in row and "step 5" in row and "2026-10-16" in row
        assert "SYNTHETIC" in row.upper() and "live smoke" in row
        assert row.index("fixture_tested") < row.index("live smoke")
        assert "live_smoke_passed" not in row and "live-verified" not in row
        assert "docs/runbook.md section 9.1" in row


def test_autoscout24_it_rows_record_step_6_and_name_enabling_next() -> None:
    """Step 6 (the owner's live smoke) passed for ``autoscout24_it`` on 2026-10-17: its rows record
    both steps (5 on SYNTHETIC fixtures, 6 live) and name enabling (step 7) as the owner's next
    decision, never claim the source is enabled or active, and point to the evidence record."""
    register_row, gates_row = _source_rows("autoscout24_it")
    for row in (register_row, gates_row):
        assert "fixture_tested" in row and "2026-10-16" in row and "SYNTHETIC" in row.upper()
        assert "live_smoke_passed" in row and "2026-10-17" in row
        assert "step 7" in row and "the owner's decision" in row
        assert "`enabled: false`" in row
        assert "docs/runbook.md section 9.1" in row
        # The 49 -> 15 cards drop strongly indicates the seller-country filter; the seller
        # countries of the cards confirm it, so no row may call it verified yet.
        assert "`cy=I` strongly indicated" in row and "`cy=I` verified" not in row
    assert "Live smoke evidence" in register_row
    # The evidence record is complete (2026-10-17): the five step-6 detail ingests have their
    # revision ids, and the ids-only evidence file carries no URL or listing text.
    evidence = _text("docs/evidence/autoscout24_it_live_smoke_2026-10-17.json")
    for revision in ("faf012b2-325d-4f22-9dfc-f638cecc273c", "4cde316a-040a-4c72-8be9-b3a05e4722a2"):
        assert revision in evidence and revision in _text("docs/source_access_register.md")
    assert "://" not in evidence and "/annunci/" not in evidence
    assert "evidence log" in gates_row
    register = _text("docs/source_access_register.md")
    record = register[register.index("### Live smoke evidence") : register.index("### Synthetic fixture")]
    for evidence in (
        "25f43f8a1451f534cb49e1b93659f904f263742a",  # commit
        "46f4bdec-1a91-4702-8ecb-1dff3f5a7255",  # crawl run
        "d3c60927-9dc7-426b-8a5a-20cab1087c32",  # discovery job
        "dad6209a-1e23-4f87-a88f-e140e6da7633",  # robots revision
        "7c78475c525adef6bf88187d53e75e9eb7b4ae9fdbe4f05215fb5ddafb396478",  # robots SHA-256
        "360d0a9d-b46d-4442-bb8b-309751cde88e",  # configuration revision
        "autoscout24@1.0.0",
    ):
        assert evidence in record, evidence
    gates = _text("ACTIVATION_GATES.md")
    log = gates[gates.index("## Evidence log") :]
    assert "`autoscout24_it` -> `live_smoke_passed`" in log and "46f4bdec-1a91-4702-8ecb-1dff3f5a7255" in log


def _shell_commands(block: str) -> list[str]:
    """The commands of a bash block, with backslash continuations joined."""
    joined = block.replace("\\\n", " ")
    return [line.strip() for line in joined.splitlines() if line.strip() and not line.strip().startswith("#")]


def test_runbook_autoscout24_smoke_commands_match_the_real_cli() -> None:
    """The owner's live smoke (runbook 9.1) may only use commands and options the CLI really has."""
    runbook = _text("docs/runbook.md")
    section = runbook[runbook.index("### 9.1 AutoScout24 live smoke") : runbook.index("## 10.")]
    block = section[
        section.index("```bash") + len("```bash") : section.index("```", section.index("```bash") + 7)
    ]
    commands = [c for c in _shell_commands(block) if "suv-deals " in c]
    used: set[str] = set()
    for line in commands:
        tokens = shlex.split(line.split("suv-deals ", 1)[1], comments=True)
        command: click.Command = cli
        path: list[str] = []
        while isinstance(command, click.Group) and tokens and tokens[0] in command.commands:
            path.append(tokens[0])
            command = command.commands[tokens.pop(0)]
        assert not isinstance(command, click.Group), line
        options = {opt for param in command.params for opt in (*param.opts, *param.secondary_opts)}
        for token in tokens:
            if token.startswith("-"):
                assert token.split("=", 1)[0] in options, (line, token)
        used.add(" ".join(path))
    assert {"sources sync", "sources inspect", "crawl once", "worker", "doctor"} <= used
    # The network switch is per command, the source switch a local uncommitted edit undone at the end.
    smoke = [c for c in commands if " crawl once " in f" {c} " or " worker " in f" {c} "]
    assert smoke and all("SOURCE_NETWORK_ENABLED=true" in c and "CRAWL4AI_BASE_URL=" in c for c in smoke), (
        smoke
    )
    assert "crawl once --source autoscout24_it --max-pages 1" in block
    # The detail drain is bounded by --max-jobs: max_detail_jobs_per_run never bounds a drain
    # (each detail job is its own run), so the runbook must not claim it does. --source keeps
    # the drain on the smoke source: other sources' queued detail jobs are never claimed.
    assert "worker --drain --queues detail --source autoscout24_it --max-jobs 5" in block
    assert "worker --drain --queues detail --source autoscout24_de" in section
    assert "at most `max_detail_jobs_per_run`" not in section
    # The spec-31 evidence ids come from a documented read-only command.
    assert "sources smoke-evidence autoscout24_it" in block and "`sources smoke-evidence` JSON" in section
    # The one-page smoke requests only the verified slug, and pageQuery is never recorded.
    assert "ONLY `/lst/renault/captur`" in section and "does NOT show whether .de applies" in section
    assert "sources sync --yes" in block and "sources inspect autoscout24_it --from-db" in block
    assert "git checkout -- config/sources/autoscout24_it.yaml" in block
    assert block.index("enabled: true") < block.index("crawl once") < block.index("git checkout --")


@pytest.mark.parametrize("relative", ["docs/seller_email_activation.md", "docs/runbook.md"])
def test_vehicle_documents_are_a_documented_manual_step(relative: str) -> None:
    text = _text(relative)
    assert "Known limitations" in text
    assert re.search(r"CoC.*manual|manual.*CoC", text, re.S | re.I)
