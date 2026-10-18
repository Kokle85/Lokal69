"""``credentials create-mcp --authorization-file`` (SYNTHETIC credentials; no network).

The token goes into a NEW 0600 file as exactly ``Bearer <token>`` and is never printed or logged.
Every refusal is a usage error raised before the database is touched: the refusal tests run with
an unreachable ``DATABASE_URL``, so a command that reached the database would end with exit 4
instead of 2. The ``db`` tests cover the written file, the rollback when writing fails, the
revocation when the commit outcome is unknown and the notice when the owner interrupts the COMMIT.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import stat
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import anyio
import pytest
from click.testing import Result
from pydantic import SecretStr
from tests.cli.conftest import Cli
from tests.integration.db.helpers import Seed

from suv_deals.cli_commands import _authorization_file, credentials
from suv_deals.cli_commands._common import CliAbort
from suv_deals.errors import DependencyUnavailable
from suv_deals.persistence import transactions
from suv_deals.settings import REPO_ROOT

UNREACHABLE_ENV = {
    "APP_ENV": "test",
    "DATABASE_URL": "postgresql://suv:suv@127.0.0.1:1/synthetic_unreachable",
    "DATABASE_SET_ROLE": "suv_backend",
    "BUILD_ID": "cli-test",
}


def create_args(workspace: UUID, path: Path | str, *extra: str) -> tuple[str, ...]:
    return (
        "credentials",
        "create-mcp",
        "--workspace",
        str(workspace),
        "--label",
        "dot via tunnel (synthetic)",
        "--scopes",
        "deals:read,reviews:read",
        "--role",
        "viewer",
        "--expires",
        "30d",
        "--authorization-file",
        str(path),
        *extra,
    )


def secret_dir(tmp_path: Path, name: str = "secrets", mode: int = 0o700) -> Path:
    directory = tmp_path / name
    directory.mkdir()
    directory.chmod(mode)
    return directory


def refused(run_cli: Cli, path: Path | str, *extra: str) -> Result:
    result = run_cli(*create_args(uuid4(), path, *extra, "--yes"), env=UNREACHABLE_ENV)
    assert result.exit_code == 2, result.output  # usage error, before any database access
    assert "MCP credential to create" not in result.output
    return result


# --------------------------------------------------------------------------------------------
# Refusals (no database)
# --------------------------------------------------------------------------------------------


def test_an_existing_file_is_never_overwritten(run_cli: Cli, tmp_path: Path) -> None:
    directory = secret_dir(tmp_path)
    existing = directory / "dot.authorization"
    existing.write_text("keep me", encoding="ascii")
    result = refused(run_cli, existing)
    assert "already exists" in result.output and "never overwritten" in result.output
    assert existing.read_text(encoding="ascii") == "keep me"


def test_symlinks_are_refused_dangling_or_not(run_cli: Cli, tmp_path: Path) -> None:
    directory = secret_dir(tmp_path)
    dangling = directory / "dangling"
    dangling.symlink_to(directory / "nowhere")
    assert "is a symlink" in refused(run_cli, dangling).output
    assert not (directory / "nowhere").exists()
    target = directory / "real"
    target.write_text("x", encoding="ascii")
    pointing = directory / "pointing"
    pointing.symlink_to(target)
    assert "is a symlink" in refused(run_cli, pointing).output
    assert target.read_text(encoding="ascii") == "x"


def test_paths_inside_the_repository_are_refused(run_cli: Cli, tmp_path: Path) -> None:
    inside = REPO_ROOT / "dot.authorization.synthetic"
    result = refused(run_cli, inside)
    assert "must be outside the repository" in result.output
    assert not inside.exists()
    # A symlinked directory leading into the checkout does not help (symlinks are resolved).
    directory = secret_dir(tmp_path)
    (directory / "repo-link").symlink_to(REPO_ROOT / "docs", target_is_directory=True)
    linked = directory / "repo-link" / "dot.authorization.synthetic"
    assert "must be outside the repository" in refused(run_cli, linked).output
    assert not (REPO_ROOT / "docs" / "dot.authorization.synthetic").exists()


def test_paths_inside_the_current_working_tree_are_refused(
    run_cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "other-checkout"
    (tree / ".git").mkdir(parents=True)
    sub = secret_dir(tree, "private")
    monkeypatch.chdir(sub)
    result = refused(run_cli, "dot.authorization")  # relative to the current directory
    assert f"must be outside the repository ({tree.resolve()})" in result.output
    assert not (sub / "dot.authorization").exists()


def test_the_parent_directory_must_exist(run_cli: Cli, tmp_path: Path) -> None:
    result = refused(run_cli, tmp_path / "missing" / "dot.authorization")
    assert "parent directory of --authorization-file does not exist" in result.output
    assert "mode 0700" in result.output
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("mode", [0o750, 0o740, 0o770, 0o705, 0o702, 0o777])
def test_the_parent_directory_must_not_be_group_or_world_accessible(
    run_cli: Cli, tmp_path: Path, mode: int
) -> None:
    directory = secret_dir(tmp_path, mode=mode)
    result = refused(run_cli, directory / "dot.authorization")
    assert f"readable or writable by group or others (mode {mode:04o})" in result.output
    assert not (directory / "dot.authorization").exists()


def test_the_parent_directory_must_belong_to_the_current_user(
    run_cli: Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = secret_dir(tmp_path)
    owner = directory.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    result = refused(run_cli, directory / "dot.authorization")
    assert "is not owned by the current user" in result.output
    assert not (directory / "dot.authorization").exists()


def test_a_refusal_comes_before_the_yes_check_and_a_dry_run_creates_nothing(
    run_cli: Cli, tmp_path: Path
) -> None:
    directory = secret_dir(tmp_path)
    existing = directory / "existing"
    existing.write_text("x", encoding="ascii")
    no_yes = run_cli(*create_args(uuid4(), existing), env=UNREACHABLE_ENV)
    assert no_yes.exit_code == 2 and "already exists" in no_yes.output
    fresh = directory / "fresh"
    dry = run_cli(*create_args(uuid4(), fresh), env=UNREACHABLE_ENV)
    assert dry.exit_code == 3, dry.output  # needs --yes
    assert f"token goes to  : {fresh}" in dry.output
    assert not fresh.exists()


def test_a_path_that_appears_after_the_check_is_refused(tmp_path: Path) -> None:
    directory = secret_dir(tmp_path)
    target = _authorization_file.prepare_target(directory / "late")
    (directory / "late").write_text("someone else", encoding="ascii")
    with pytest.raises(CliAbort) as raised:
        _authorization_file.ReservedFile(target)
    assert raised.value.code == 2 and "already exists" in raised.value.message
    assert (directory / "late").read_text(encoding="ascii") == "someone else"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o707], ids=lambda mode: f"{mode:04o}")
def test_a_directory_opened_up_after_the_check_is_refused(tmp_path: Path, mode: int) -> None:
    directory = secret_dir(tmp_path)
    target = _authorization_file.prepare_target(directory / "late")
    directory.chmod(mode)  # between the check and the creation: the opened directory is checked again
    with pytest.raises(CliAbort) as raised:
        _authorization_file.ReservedFile(target)
    assert raised.value.code == 2
    assert f"readable or writable by group or others (mode {mode:04o})" in raised.value.message
    assert not (directory / "late").exists()


def test_an_interrupt_after_the_write_keeps_the_file_and_names_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-C makes asyncio cancel the main task; the cancellation lands in the COMMIT, after the
    token is on disk, so the outcome is unknown: the file is kept and the credential named."""
    path = secret_dir(tmp_path) / "dot.authorization"
    target = _authorization_file.prepare_target(path)
    token = "suvmcp_" + "9e" * 32
    issued = SimpleNamespace(credential_id=uuid4(), token=SecretStr(token))

    async def issue(conn: Any) -> Any:
        return issued

    async def main() -> bool:
        with anyio.CancelScope() as scope:

            @asynccontextmanager
            async def interrupted_commit(db: Any, actor: Any) -> AsyncIterator[Any]:
                yield object()
                scope.cancel()  # Ctrl-C ...
                await anyio.sleep(30)  # ... while the COMMIT round-trip is in flight

            monkeypatch.setattr(transactions, "unit_of_work", interrupted_commit)
            await credentials._issue_into_file(cast(Any, None), cast(Any, None), target, issue)
        return scope.cancelled_caught

    assert anyio.run(main) is True  # the interrupt propagated (the command then reports it)
    err = capsys.readouterr().err
    assert f"credential {issued.credential_id} may be live" in err
    assert f"The authorization file {path} was KEPT because its token may be live" in err
    assert f"suv-deals credentials revoke {issued.credential_id} --reason" in err
    assert f"mcp probe --authorization-file {path}" in err
    assert path.read_text(encoding="ascii") == f"Bearer {token}" and token not in err
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_interrupt_before_the_write_removes_the_file_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = secret_dir(tmp_path) / "dot.authorization"
    target = _authorization_file.prepare_target(path)

    async def interrupted_insert(conn: Any) -> Any:
        raise KeyboardInterrupt  # before the token exists: nothing was committed

    @asynccontextmanager
    async def plain(db: Any, actor: Any) -> AsyncIterator[Any]:
        yield object()

    monkeypatch.setattr(transactions, "unit_of_work", plain)
    with pytest.raises(KeyboardInterrupt):
        anyio.run(credentials._issue_into_file, cast(Any, None), cast(Any, None), target, interrupted_insert)
    assert not path.exists()
    assert capsys.readouterr().err == ""


def test_reserved_file_is_0600_whatever_the_umask(tmp_path: Path) -> None:
    directory = secret_dir(tmp_path)
    for umask in (0o000, 0o077, 0o777):
        previous = os.umask(umask)
        try:
            reserved = _authorization_file.ReservedFile(
                _authorization_file.prepare_target(directory / f"umask-{umask:o}")
            )
            reserved.write("Bearer suvmcp_" + "cd" * 32)
            reserved.close()
        finally:
            os.umask(previous)
        path = directory / f"umask-{umask:o}"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert path.read_bytes() == b"Bearer suvmcp_" + b"cd" * 32
    discarded = _authorization_file.ReservedFile(_authorization_file.prepare_target(directory / "gone"))
    discarded.write("Bearer x")
    discarded.discard()
    discarded.discard()  # idempotent
    assert not (directory / "gone").exists()


# --------------------------------------------------------------------------------------------
# With the database (marker db)
# --------------------------------------------------------------------------------------------


@contextmanager
def umask(value: int) -> Iterator[None]:
    previous = os.umask(value)
    try:
        yield
    finally:
        os.umask(previous)


def _credentials(seed: Seed, workspace: UUID) -> list[tuple[Any, ...]]:
    return seed.conn.execute(
        "select id, token_hash, token_prefix, scopes, role, revoked_at, revoke_reason"
        " from ops.api_credentials where workspace_id = %s",
        (workspace,),
    ).fetchall()


@pytest.mark.db
def test_the_token_is_written_to_the_file_and_never_printed(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    path = secret_dir(tmp_path) / "dot.authorization"
    with umask(0o000):  # 0600 must not depend on the umask
        result = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert result.exit_code == 0, result.output

    content = path.read_bytes()
    assert content.startswith(b"Bearer suvmcp_") and not content.endswith(b"\n")
    token = content.decode("ascii").removeprefix("Bearer ")
    assert len(token) == len("suvmcp_") + 64
    info = os.lstat(path)
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    assert info.st_uid == os.geteuid()

    rows = _credentials(seed, workspace)
    assert len(rows) == 1
    _, token_hash, prefix, scopes, role, revoked_at, _ = rows[0]
    assert token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token.startswith(prefix) and scopes == ["deals:read", "reviews:read"] and role == "viewer"
    assert revoked_at is None

    assert token not in result.stdout and token not in result.stderr and token not in result.output
    assert token not in caplog.text
    assert f"token goes to  : {path}" in result.output
    assert f"Authorization header written to {path}" in result.output
    assert "The token is not shown and" in result.output
    assert "Token (shown ONCE" not in result.output
    assert "(then delete the file)" in result.output

    again = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert again.exit_code == 2 and "already exists" in again.output
    assert path.read_bytes() == content and len(_credentials(seed, workspace)) == 1


@pytest.mark.db
def test_a_refusal_creates_no_credential(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    open_dir = secret_dir(tmp_path, mode=0o755)
    result = run_cli(*create_args(workspace, open_dir / "dot.authorization", "--yes"), env=db_env)
    assert result.exit_code == 2, result.output
    assert _credentials(seed, workspace) == []


@pytest.mark.db
def test_a_failed_write_rolls_the_credential_back(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disk_full(self: _authorization_file.ReservedFile, value: str) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(_authorization_file.ReservedFile, "write", disk_full)
    path = secret_dir(tmp_path) / "dot.authorization"
    result = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert result.exit_code == 1, result.output
    assert "could not write the authorization file" in result.output
    assert "No space left on device" in result.output
    assert "rolled back, so no credential was created" in result.output
    assert not path.exists()
    assert _credentials(seed, workspace) == []
    audits = seed.scalar(
        "select count(*) from ops.audit_events where workspace_id = %s and action = 'credential.create'",
        (workspace,),
    )
    assert audits == 0


def _flaky_unit_of_work(monkeypatch: pytest.MonkeyPatch, *, after_commit: bool, revoke_fails: bool) -> None:
    """``unit_of_work`` whose FIRST use fails at COMMIT time: after a real commit (outcome unknown
    to the caller, e.g. a dropped connection) or before it (rolled back)."""
    real = transactions.unit_of_work
    calls = {"n": 0}

    @asynccontextmanager
    async def flaky(db: Any, actor: Any) -> AsyncIterator[Any]:
        calls["n"] += 1
        if calls["n"] > 1 and revoke_fails:
            raise DependencyUnavailable("database unavailable")
        if calls["n"] > 1:
            async with real(db, actor) as conn:
                yield conn
            return
        if after_commit:
            async with real(db, actor) as conn:
                yield conn
            raise DependencyUnavailable("database unavailable")  # committed, but the caller never knows
        async with real(db, actor) as conn:
            yield conn
            raise DependencyUnavailable("database unavailable")  # inside: the transaction rolls back

    monkeypatch.setattr(transactions, "unit_of_work", flaky)


@pytest.mark.db
def test_an_unknown_commit_outcome_revokes_the_credential(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flaky_unit_of_work(monkeypatch, after_commit=True, revoke_fails=False)
    path = secret_dir(tmp_path) / "dot.authorization"
    result = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert result.exit_code == 1, result.output
    assert "commit outcome was unknown" in result.output and "existed and was revoked" in result.output
    assert "the authorization file was removed" in result.output
    assert not path.exists()
    rows = _credentials(seed, workspace)
    assert len(rows) == 1
    assert rows[0][5] is not None  # revoked_at: no live token that nobody holds
    assert rows[0][6] == "authorization file not confirmed: commit outcome unknown"


@pytest.mark.db
def test_a_rolled_back_commit_leaves_nothing(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flaky_unit_of_work(monkeypatch, after_commit=False, revoke_fails=False)
    path = secret_dir(tmp_path) / "dot.authorization"
    result = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert result.exit_code == 1, result.output
    assert "the credential was not committed" in result.output and "none exists" in result.output
    assert not path.exists()
    assert _credentials(seed, workspace) == []


@pytest.mark.db
def test_a_live_token_that_cannot_be_revoked_keeps_its_file(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flaky_unit_of_work(monkeypatch, after_commit=True, revoke_fails=True)
    path = secret_dir(tmp_path) / "dot.authorization"
    result = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert result.exit_code == 1, result.output
    rows = _credentials(seed, workspace)
    assert len(rows) == 1 and rows[0][5] is None  # committed and still live
    credential_id = rows[0][0]
    assert f"credential {credential_id} could not be revoked" in result.output
    assert f"suv-deals credentials revoke {credential_id}" in result.output
    assert "was KEPT because its token may be live" in result.output
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    token = path.read_text(encoding="ascii").removeprefix("Bearer ")
    assert hashlib.sha256(token.encode()).hexdigest() == rows[0][1]
    assert token not in result.output


@pytest.mark.db
def test_an_interrupt_after_the_commit_keeps_the_file_and_prints_the_revoke_command(
    run_cli: Cli,
    db_env: dict[str, str],
    workspace: UUID,
    seed: Seed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = transactions.unit_of_work

    @asynccontextmanager
    async def committed_then_interrupted(db: Any, actor: Any) -> AsyncIterator[Any]:
        async with real(db, actor) as conn:
            yield conn
        raise KeyboardInterrupt  # Ctrl-C while the COMMIT acknowledgement is awaited

    monkeypatch.setattr(transactions, "unit_of_work", committed_then_interrupted)
    path = secret_dir(tmp_path) / "dot.authorization"
    result = run_cli(*create_args(workspace, path, "--yes"), env=db_env)
    assert result.exit_code == 1, result.output
    assert "error: interrupted" in result.stderr
    rows = _credentials(seed, workspace)
    assert len(rows) == 1 and rows[0][5] is None  # committed and live
    credential_id = rows[0][0]
    assert f"credential {credential_id} may be live" in result.stderr
    assert f"The authorization file {path} was KEPT because its token may be live" in result.stderr
    assert f"suv-deals credentials revoke {credential_id} --reason" in result.stderr
    token = path.read_text(encoding="ascii").removeprefix("Bearer ")
    assert hashlib.sha256(token.encode()).hexdigest() == rows[0][1]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert token not in result.output


@pytest.mark.db
def test_a_refused_insert_leaves_no_file(
    run_cli: Cli, db_env: dict[str, str], workspace: UUID, seed: Seed, tmp_path: Path
) -> None:
    path = secret_dir(tmp_path) / "dot.authorization"
    args = list(create_args(workspace, path, "--yes"))
    args[args.index("deals:read,reviews:read")] = "reviews:write"  # wider than the viewer role
    result = run_cli(*args, env=db_env)
    assert result.exit_code == 1, result.output
    assert "VALIDATION_ERROR" in result.output
    assert not path.exists()
    assert _credentials(seed, workspace) == []
