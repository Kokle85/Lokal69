"""F6 / OPS-08 (wave D2): the exact-build release verifier covers every shipped component.

Before D2 ``scripts/verify_release.sh`` linted and type-checked only ``src tests scripts`` /
``suv_deals``, never ran the desktop worker's tests (the component installed on the owner's PC),
the dashboard build/lint/tests/audit or the browser E2E, and could still print
``result=verified``. Now every step is recorded as passed / FAILED / NOT RUN, the browser E2E runs
only with ``--with-e2e`` (otherwise NOT RUN, so the result is never "verified" without it), and
the node/npm versions are part of the release record.

The run test uses stub ``uv``/``npm``/``make``/``node`` executables on ``PATH`` and a temporary
report directory: nothing is installed, built or contacted.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "verify_release.sh"


def _steps() -> dict[str, str]:
    text = SCRIPT.read_text(encoding="utf-8")
    block = re.search(r"^steps=\(\n(.*?)\n\)", text, re.S | re.M)
    assert block is not None
    steps: dict[str, str] = {}
    for line in block.group(1).splitlines():
        match = re.fullmatch(r'\s*"([a-z0-9_]+)\|(.*)"', line)
        assert match is not None, line
        steps[match.group(1)] = match.group(2)
    return steps


def test_every_component_has_a_release_step() -> None:
    steps = _steps()
    assert "desktop/outlook-bridge" in steps["lint"]
    assert "ruff format --check src tests scripts desktop/outlook-bridge" in steps["lint"]
    assert "mypy --strict tests/e2e" in steps["typecheck"]
    assert "MYPYPATH=desktop/outlook-bridge" in steps["typecheck"]
    assert "-p outlook_bridge" in steps["typecheck"]
    assert "pytest -q desktop/outlook-bridge/tests" in steps["desktop_tests"]
    assert steps["dashboard_ci"] == "npm --prefix dashboard ci"
    assert steps["dashboard_build"] == "npm --prefix dashboard run build"
    assert steps["dashboard_test"] == "npm --prefix dashboard test"
    assert steps["dashboard_lint"] == "npm --prefix dashboard run lint"
    assert steps["dashboard_audit"] == "npm --prefix dashboard audit --omit=dev"
    assert steps["e2e_pg16"].startswith("make e2e ") and "PG16_ADMIN_URL" in steps["e2e_pg16"]
    assert steps["e2e_pg17"].startswith("make e2e ") and "PG17_ADMIN_URL" in steps["e2e_pg17"]


DASHBOARD_STEPS = ("dashboard_ci", "dashboard_build", "dashboard_test", "dashboard_lint", "dashboard_audit")
E2E_STEPS = ("e2e_pg16", "e2e_pg17")


@pytest.mark.parametrize(
    ("args", "not_run"),
    [
        ([], dict.fromkeys(E2E_STEPS, "--with-e2e")),
        (["--with-e2e"], {}),
        (["--skip-dashboard", "--with-e2e"], dict.fromkeys(DASHBOARD_STEPS + E2E_STEPS, "--skip-dashboard")),
        (["--skip-db", "--with-e2e"], dict.fromkeys(("tests_db_pg16", "tests_db_pg17"), "--skip-db")),
    ],
)
def test_plan_marks_skipped_steps_not_run(args: list[str], not_run: dict[str, str]) -> None:
    result = subprocess.run(
        ["bash", str(SCRIPT), "--plan", *args], capture_output=True, text=True, check=False, timeout=30
    )
    assert result.returncode == 0, result.stderr
    planned = {
        line.split(":")[0].strip(): line for line in result.stdout.splitlines() if line.startswith("  ")
    }
    for name in _steps():
        line = planned[name]
        if name in not_run:
            assert "NOT RUN" in line and not_run[name] in line, line
        else:
            assert "NOT RUN" not in line, line


def _stub(directory: Path, name: str, body: str) -> None:
    path = directory / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def test_a_run_records_every_step_and_the_node_versions(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    for tool in ("uv", "make"):
        _stub(bin_dir, tool, f'echo "{tool} $*" >> "{calls}"; exit 0')
    _stub(
        bin_dir,
        "npm",
        f'if [ "$1" = "--version" ]; then echo 10.9.9; exit 0; fi\necho "npm $*" >> "{calls}"; exit 0',
    )
    _stub(bin_dir, "node", 'echo "v22.99.0"')
    reports = tmp_path / "reports"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RELEASE_REPORT_DIR": str(reports)}
    result = subprocess.run(
        ["bash", str(SCRIPT), "--with-e2e"], capture_output=True, text=True, env=env, check=False, timeout=60
    )
    [report] = list(reports.iterdir())
    text = report.read_text(encoding="utf-8")
    assert "node_version=v22.99.0" in text and "npm_version=10.9.9" in text
    for name in _steps():
        assert f"  {name}=passed" in text, (name, text)
    made = calls.read_text(encoding="utf-8")
    assert "npm --prefix dashboard ci" in made and "npm --prefix dashboard audit --omit=dev" in made
    assert "make e2e PG16_ADMIN_URL=" in made and "pytest -q desktop/outlook-bridge/tests" in made
    assert "mypy --strict -p outlook_bridge" in made
    # The working tree decides releasability (a dirty tree is never "verified").
    assert re.search(r"^result=(verified|not releasable)$", text, re.M)
    assert result.returncode == 0
    assert not (REPO / "var" / "releases" / report.name).exists()


def test_without_e2e_the_release_is_never_verified(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("uv", "make", "npm", "node"):
        _stub(bin_dir, tool, "exit 0")
    reports = tmp_path / "reports"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "RELEASE_REPORT_DIR": str(reports)}
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, check=False, timeout=60
    )
    [report] = list(reports.iterdir())
    text = report.read_text(encoding="utf-8")
    assert "  e2e_pg16=NOT RUN (pass --with-e2e)" in text
    assert "result=verified" not in text
    assert result.returncode == 1
