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
from pathlib import Path

import pytest

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
    assert "pytest marker" not in step
    assert "insufficient_comparables" in register  # F2: no MK evidence -> no valuation


@pytest.mark.parametrize("relative", ["docs/seller_email_activation.md", "docs/runbook.md"])
def test_vehicle_documents_are_a_documented_manual_step(relative: str) -> None:
    text = _text(relative)
    assert "Known limitations" in text
    assert re.search(r"CoC.*manual|manual.*CoC", text, re.S | re.I)
