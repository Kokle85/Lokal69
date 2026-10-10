"""Command line: ``check``, ``run``, ``status``, ``reconcile-once`` and ``credential``.

``python -m outlook_bridge [--config PATH] <command> [--dry-run]``

- ``check``           read-only environment report: configuration, classic-vs-new Outlook,
                      interactive-session guard, shared reply semantics, credential presence.
- ``run``             the worker loop (refuses non-classic Outlook, SYSTEM/service/
                      non-interactive sessions and a missing/unusable credential).
- ``reconcile-once``  one startup cycle (binding sync, reconciliation, uploads, sends) and exit.
- ``status``          local health and coverage gaps from the protected store (no Outlook needed).
- ``credential set|delete|status``  manage the narrow worker credential in the OS credential
                      store; the secret is read from the terminal/stdin, never from arguments.

``--dry-run`` (run/reconcile-once) uses a throw-away in-memory store, may sync bindings (GET)
and correlates locally, but never uploads, reports, sends heartbeats or calls ``MailItem.Send``.
Output is JSON without mail content.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, TextIO

import httpx

from outlook_bridge import WORKER_SOFTWARE
from outlook_bridge.api_client import BridgeApiClient, ClientIdentity
from outlook_bridge.compatibility import (
    CompatibilityReport,
    ProcessProbe,
    RegistryReader,
    check_compatibility,
    running_process_names,
)
from outlook_bridge.config import (
    CONFIG_FILENAME,
    BridgeConfig,
    default_data_dir,
    ensure_private_dir,
    load_config,
)
from outlook_bridge.credentials import (
    CredentialManager,
    CredentialState,
    CredentialStore,
    WindowsCredentialManagerStore,
    WorkerCredential,
)
from outlook_bridge.errors import BridgeError, ConfigError, CredentialError, NonInteractiveSession, StaError
from outlook_bridge.health import build_health
from outlook_bridge.local_queue import LocalStore
from outlook_bridge.log import configure_logging
from outlook_bridge.matching import semantics_versions
from outlook_bridge.outlook_adapter import (
    MailboxAdapter,
    OutlookComSession,
    StaMailbox,
    private_temp_dir,
    real_com_bindings,
)
from outlook_bridge.sta_runtime import (
    PythonComApi,
    SessionContext,
    StaExecutor,
    probe_session_context,
    session_refusal_reasons,
)
from outlook_bridge.worker import BridgeWorker
from suv_deals.clock import Clock, SystemClock, ensure_utc

EXIT_OK: Final = 0
EXIT_CONFIG: Final = 2
EXIT_UNSUPPORTED: Final = 3
EXIT_CREDENTIAL: Final = 4
EXIT_RUNTIME: Final = 5

MailboxFactory = Callable[[BridgeConfig, SessionContext, Clock], tuple[MailboxAdapter, Callable[[], None]]]


def _default_mailbox_factory(
    config: BridgeConfig, session: SessionContext, clock: Clock
) -> tuple[MailboxAdapter, Callable[[], None]]:  # pragma: no cover - requires Windows/pywin32
    executor = StaExecutor(PythonComApi(), session)  # refuses SYSTEM/service/non-interactive sessions
    executor.start()
    attach, bind_events = real_com_bindings(start_if_not_running=config.start_outlook_if_not_running)
    session_obj = OutlookComSession(
        attach=attach,
        bind_events=bind_events,
        temp_dir=private_temp_dir(config.resolved_data_dir()),
        now=clock.now,
    )
    return StaMailbox(executor, session_obj), executor.stop


def _default_credential_store(config: BridgeConfig) -> CredentialStore:  # pragma: no cover - Windows only
    return WindowsCredentialManagerStore(config.credential_target())


@dataclass
class CliDeps:
    """Injectable environment (tests replace every Windows-specific part)."""

    platform: str = field(default_factory=lambda: sys.platform)
    session_probe: Callable[[], SessionContext] = probe_session_context
    registry: RegistryReader | None = None
    processes: ProcessProbe | None = None
    credential_store: Callable[[BridgeConfig], CredentialStore] = _default_credential_store
    mailbox_factory: MailboxFactory = _default_mailbox_factory
    transport: httpx.BaseTransport | None = None
    clock: Clock = field(default_factory=SystemClock)
    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    read_secret: Callable[[str], str] = getpass.getpass
    stop_event: threading.Event | None = None
    max_ticks: int | None = None
    configure_logs: bool = True


class _MemoryRuntime:
    """Rejection memory when no local store exists yet."""

    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    def get_runtime(self, key: str) -> str | None:
        return self._values.get(key)

    def set_runtime(self, key: str, value: str) -> None:
        self._values[key] = value


def _print(deps: CliDeps, payload: dict[str, Any]) -> None:
    deps.out.write(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="outlook_bridge", description="Local classic-Outlook reply worker")
    parser.add_argument("--config", type=Path, default=None, help=f"path to {CONFIG_FILENAME}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="read-only environment and configuration report")
    run = commands.add_parser("run", help="run the worker loop")
    run.add_argument("--dry-run", action="store_true")
    once = commands.add_parser("reconcile-once", help="one startup cycle, then exit")
    once.add_argument("--dry-run", action="store_true")
    commands.add_parser("status", help="local health and coverage gaps")
    credential = commands.add_parser("credential", help="manage the worker credential")
    credential.add_argument("action", choices=("set", "delete", "status"))
    credential.add_argument("--expires-at", default=None, help="ISO-8601 expiry stated at activation")
    return parser


def _config_path(arg: Path | None, deps: CliDeps) -> Path:
    if arg is not None:
        return arg
    return default_data_dir(platform=deps.platform) / CONFIG_FILENAME


def _compat(deps: CliDeps, now: datetime) -> CompatibilityReport:
    processes = deps.processes
    if processes is None and deps.platform == "win32":  # pragma: no cover - Windows only
        processes = running_process_names
    return check_compatibility(now=now, platform=deps.platform, registry=deps.registry, processes=processes)


def _semantics() -> dict[str, Any]:
    """Versions of the shared reply semantics in use (they are a hard dependency)."""
    return {"available": True, **semantics_versions()}


def main(argv: Sequence[str] | None = None, deps: CliDeps | None = None) -> int:
    deps = deps or CliDeps()
    args = _parser().parse_args(argv)
    try:
        if args.command == "check":
            return _check(args, deps)
        config = load_config(_config_path(args.config, deps))
        if args.command == "status":
            return _status(config, deps)
        if args.command == "credential":
            return _credential(config, deps, args.action, args.expires_at)
        return _run(config, deps, once=args.command == "reconcile-once", dry_run=bool(args.dry_run))
    except ConfigError as exc:
        deps.err.write(f"configuration error: {exc}\n")
        return EXIT_CONFIG
    except NonInteractiveSession as exc:
        deps.err.write(f"refused: {exc}\n")
        return EXIT_UNSUPPORTED
    except CredentialError as exc:
        deps.err.write(f"credential: {exc}\n")
        return EXIT_CREDENTIAL
    except BridgeError as exc:
        deps.err.write(f"error ({exc.code}): {exc}\n")
        return EXIT_RUNTIME


def _check(args: argparse.Namespace, deps: CliDeps) -> int:
    now = deps.clock.now()
    report: dict[str, Any] = {"software": WORKER_SOFTWARE, "checked_at": now.isoformat()}
    code = EXIT_OK
    config: BridgeConfig | None = None
    try:
        config = load_config(_config_path(args.config, deps))
        report["config"] = {
            "valid": True,
            "mailbox_binding_id": str(config.mailbox_binding_id),
            "folders": [spec.role for spec in config.folders],
            "reconcile_interval_seconds": config.reconcile_interval_seconds,
        }
    except ConfigError as exc:
        report["config"] = {"valid": False, "problem": str(exc)}
        code = EXIT_CONFIG
    compat = _compat(deps, now)
    report["compatibility"] = compat.model_dump(mode="json")
    session = deps.session_probe()
    reasons = session_refusal_reasons(session)
    report["session"] = {"interactive_user": not reasons, "refusal_reasons": list(reasons)}
    if code == EXIT_OK and (not compat.supported or reasons):
        code = EXIT_UNSUPPORTED
    report["reply_semantics"] = _semantics()
    if config is not None:
        try:
            manager = CredentialManager(deps.credential_store(config), _MemoryRuntime())
            state = manager.state(now)
        except Exception:
            state = CredentialState.MISSING
        report["credential"] = {"state": state.value}
        if code == EXIT_OK and state != CredentialState.ACTIVE:
            code = EXIT_CREDENTIAL
    report["ready"] = code == EXIT_OK
    report["settings_modified"] = False
    _print(deps, report)
    return code


def client_identity(config: BridgeConfig, store: LocalStore) -> ClientIdentity:
    """The API identity of this installation: the configured worker id plus the local store's
    instance id, which every send-intent claim carries (SEC-1: one running intent, one store)."""
    return ClientIdentity(config.mailbox_binding_id, config.worker_id, store.store_instance_id)


def _open_store(config: BridgeConfig, *, dry_run: bool) -> LocalStore:
    if dry_run:
        return LocalStore.in_memory(config.mailbox_binding_id)
    ensure_private_dir(config.resolved_data_dir())
    return LocalStore.open(config.store_path(), config.mailbox_binding_id)


def _status(config: BridgeConfig, deps: CliDeps) -> int:
    now = deps.clock.now()
    if not config.store_path().exists():
        _print(deps, {"status": "no local state yet", "monitoring_24h_claimed": False})
        return EXIT_OK
    store = LocalStore.open(config.store_path(), config.mailbox_binding_id)
    try:
        manager = CredentialManager(deps.credential_store(config), store)
        snapshot = build_health(
            store, config=config, now=now, connection=None, compat=None, credential_state=manager.state(now)
        )
        _print(deps, snapshot.model_dump(mode="json"))
    finally:
        store.close()
    return EXIT_OK


def _credential(config: BridgeConfig, deps: CliDeps, action: str, expires_at: str | None) -> int:
    store = deps.credential_store(config)
    manager = CredentialManager(store, _MemoryRuntime())
    now = deps.clock.now()
    if action == "set":
        token = deps.read_secret("Mail-worker credential (input hidden): ").strip()
        expiry = None
        if expires_at:
            try:
                expiry = ensure_utc(datetime.fromisoformat(expires_at))
            except ValueError:
                raise ConfigError("--expires-at must be an ISO-8601 timestamp with a time zone") from None
        credential = WorkerCredential(token=token, expires_at=expiry)
        manager.replace(credential)
        _print(deps, {"stored": True, "fingerprint": credential.fingerprint, "expires_at": expiry})
        return EXIT_OK
    if action == "delete":
        manager.delete()
        _print(deps, {"deleted": True})
        return EXIT_OK
    loaded = store.load()
    _print(
        deps,
        {
            "state": manager.state(now).value,
            "fingerprint": loaded.fingerprint if loaded else None,
            "expires_at": loaded.expires_at if loaded else None,
        },
    )
    return EXIT_OK


def _run(config: BridgeConfig, deps: CliDeps, *, once: bool, dry_run: bool) -> int:
    now = deps.clock.now()
    if deps.configure_logs:
        configure_logging(None if dry_run else config.resolved_data_dir() / "logs")
    compat = _compat(deps, now)
    if not compat.supported:
        deps.err.write("unsupported Outlook environment: " + ", ".join(compat.problems) + "\n")
        _print(deps, {"compatibility": compat.model_dump(mode="json")})
        return EXIT_UNSUPPORTED
    session = deps.session_probe()
    reasons = session_refusal_reasons(session)
    if reasons:
        raise NonInteractiveSession(reasons)
    store = _open_store(config, dry_run=dry_run)
    closers: list[Callable[[], None]] = [store.close]
    try:
        credentials = CredentialManager(deps.credential_store(config), store)
        state = credentials.state(now)
        if state != CredentialState.ACTIVE and not dry_run:
            deps.err.write(f"worker credential is {state.value}; run 'credential set'\n")
            return EXIT_CREDENTIAL
        api = None
        if state == CredentialState.ACTIVE:
            api = BridgeApiClient(
                config.api_base_url,
                token_provider=lambda: credentials.token(deps.clock.now()),
                identity=client_identity(config, store),
                timeout_seconds=config.http_timeout_seconds,
                transport=deps.transport,
            )
            closers.append(api.close)
        try:
            mailbox, close_mailbox = deps.mailbox_factory(config, session, deps.clock)
        except StaError as exc:
            deps.err.write(f"cannot start the Outlook STA thread: {exc}\n")
            return EXIT_RUNTIME
        closers.append(close_mailbox)
        worker = BridgeWorker(
            config=config,
            store=store,
            mailbox=mailbox,
            api=api,
            credentials=credentials,
            clock=deps.clock,
            compat=compat,
            dry_run=dry_run,
        )
        if once:
            cycle = worker.start()
            worker.shutdown()
            _print(
                deps,
                {
                    "dry_run": dry_run,
                    "connected": bool(worker.folders),
                    "binding_pages": cycle.binding_pages,
                    "reconciled": cycle.reconciled,
                    "scan": dict(cycle.scan),
                    "uploads": dict(cycle.uploads),
                    "sends": dict(cycle.sends),
                    "health": worker.health().model_dump(mode="json"),
                },
            )
            return EXIT_OK
        stop = deps.stop_event or threading.Event()
        if threading.current_thread() is threading.main_thread():  # pragma: no cover - interactive use
            signal.signal(signal.SIGINT, lambda *_: stop.set())
            signal.signal(signal.SIGTERM, lambda *_: stop.set())
        worker.run(stop, max_ticks=deps.max_ticks)
        return EXIT_OK
    finally:
        for close in reversed(closers):
            with contextlib.suppress(Exception):  # best-effort shutdown
                close()


__all__ = [
    "EXIT_CONFIG",
    "EXIT_CREDENTIAL",
    "EXIT_OK",
    "EXIT_RUNTIME",
    "EXIT_UNSUPPORTED",
    "CliDeps",
    "MailboxFactory",
    "main",
]
