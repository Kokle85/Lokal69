"""Classic Outlook for Windows detection (spec 37.6 "Outlook compatibility and execution"; U7).

The local route needs the Outlook Object Model (COM ProgID ``Outlook.Application``), which only
*classic* Outlook for Windows provides; Microsoft lists OOM/COM add-ins/MAPI as unsupported in
*new* Outlook. This module reports - it never changes Outlook, Trust Center, registry or
antivirus settings (the registry interface it uses is read-only by construction).

Signals (all read-only):

- ``HKCR\\Outlook.Application\\CLSID`` and ``\\CurVer``: the classic Outlook COM server is
  registered (new Outlook does not register it).
- ``HKLM\\SOFTWARE\\Microsoft\\Office\\ClickToRun\\Configuration\\VersionToReport``: the
  installed Microsoft 365 / Office build when Click-to-Run is used (informational).
- ``HKCU\\Software\\Microsoft\\Office\\16.0\\Outlook\\Preferences\\UseNewOutlook`` = 1: the
  "new Outlook" toggle is on; starting Outlook then opens new Outlook, so the route is
  unsupported until the user switches back to classic Outlook.
- Running processes (optional): ``olk.exe`` (new Outlook) without ``outlook.exe`` (classic).

The exact registry locations are documented by Microsoft for current builds but must be
re-verified on the actual machine during activation (``check`` reports what it found).
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict

Hive = Literal["HKCR", "HKCU", "HKLM"]
RegistryValue = str | int | None

OUTLOOK_PROGID: Final = "Outlook.Application"
C2R_CONFIGURATION_KEY: Final = r"SOFTWARE\Microsoft\Office\ClickToRun\Configuration"
NEW_OUTLOOK_PREFERENCES_KEY: Final = r"Software\Microsoft\Office\16.0\Outlook\Preferences"
NEW_OUTLOOK_TOGGLE_VALUE: Final = "UseNewOutlook"
CLASSIC_PROCESS: Final = "outlook.exe"
NEW_OUTLOOK_PROCESS: Final = "olk.exe"


class OutlookFlavour(StrEnum):
    CLASSIC = "classic"
    NEW = "new"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


class RegistryReader(Protocol):
    """Read-only registry access (there is deliberately no write method)."""

    def read_value(self, hive: Hive, key: str, name: str | None) -> RegistryValue: ...


class ProcessProbe(Protocol):
    def __call__(self) -> frozenset[str] | None:
        """Lower-case executable names of running processes, or ``None`` when unknown."""
        ...


class WinRegReader:  # pragma: no cover - requires Windows
    """``winreg`` with ``KEY_READ`` only; missing keys/values read as ``None``."""

    def __init__(self) -> None:
        self._winreg: Any = importlib.import_module("winreg")

    def read_value(self, hive: Hive, key: str, name: str | None) -> RegistryValue:
        winreg = self._winreg
        root = {
            "HKCR": winreg.HKEY_CLASSES_ROOT,
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
        }[hive]
        views = [0] if hive != "HKLM" else [winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY]
        for view in views:
            try:
                with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as handle:
                    value, _kind = winreg.QueryValueEx(handle, name or "")
            except OSError:
                continue
            if isinstance(value, str | int):
                return value
            return None
        return None


class CompatibilityReport(BaseModel):
    """Result of ``check_compatibility``; ``settings_modified`` is always ``False``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: datetime
    platform: str
    windows: bool
    progid_registered: bool | None
    progid_clsid: str | None = None
    progid_current_version: str | None = None
    office_build: str | None = None
    new_outlook_toggle_on: bool | None = None
    classic_process_running: bool | None = None
    new_outlook_process_running: bool | None = None
    com_version: str | None = None
    flavour: OutlookFlavour
    supported: bool
    problems: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    settings_modified: Literal[False] = False

    @property
    def report_flavour(self) -> Literal["classic", "new", "unknown"]:
        """Value for the backend ``OutlookAccountReport.outlook_flavour``."""
        if self.flavour == OutlookFlavour.CLASSIC:
            return "classic"
        if self.flavour == OutlookFlavour.NEW:
            return "new"
        return "unknown"


def _as_text(value: RegistryValue) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= 256:
        return value.strip()
    return None


def check_compatibility(
    *,
    now: datetime,
    platform: str | None = None,
    registry: RegistryReader | None = None,
    processes: ProcessProbe | None = None,
    com_version_probe: Callable[[], str | None] | None = None,
) -> CompatibilityReport:
    """Report whether the classic-Outlook route can be used on this machine (read-only)."""
    plat = sys.platform if platform is None else platform
    problems: list[str] = []
    notes: list[str] = []
    if plat != "win32":
        return CompatibilityReport(
            checked_at=now,
            platform=plat,
            windows=False,
            progid_registered=None,
            flavour=OutlookFlavour.UNKNOWN,
            supported=False,
            problems=("NOT_WINDOWS",),
            notes=("classic Outlook automation is only available on Windows",),
        )
    reader = registry if registry is not None else WinRegReader()
    clsid = _as_text(reader.read_value("HKCR", rf"{OUTLOOK_PROGID}\CLSID", None))
    cur_ver = _as_text(reader.read_value("HKCR", rf"{OUTLOOK_PROGID}\CurVer", None))
    build = _as_text(reader.read_value("HKLM", C2R_CONFIGURATION_KEY, "VersionToReport"))
    toggle_raw = reader.read_value("HKCU", NEW_OUTLOOK_PREFERENCES_KEY, NEW_OUTLOOK_TOGGLE_VALUE)
    toggle_on: bool | None = None
    if isinstance(toggle_raw, int):
        toggle_on = toggle_raw == 1
    elif isinstance(toggle_raw, str):
        toggle_on = toggle_raw.strip() == "1"
    elif toggle_raw is None:
        toggle_on = False  # value absent: the toggle has never been switched on
    running = processes() if processes is not None else None
    classic_running = None if running is None else CLASSIC_PROCESS in running
    new_running = None if running is None else NEW_OUTLOOK_PROCESS in running
    registered = clsid is not None
    com_version: str | None = None
    if com_version_probe is not None:
        try:
            com_version = com_version_probe()
        except Exception:
            notes.append("live COM probe failed; Outlook may not be running")

    if not registered:
        problems.append("OUTLOOK_COM_NOT_REGISTERED")
        flavour = OutlookFlavour.NEW if new_running else OutlookFlavour.NOT_INSTALLED
        if new_running:
            problems.append("NEW_OUTLOOK_UNSUPPORTED")
    elif toggle_on:
        flavour = OutlookFlavour.NEW
        problems.append("NEW_OUTLOOK_ENABLED")
        notes.append("switch the 'New Outlook' toggle off to use classic Outlook; nothing was changed")
    elif new_running and not classic_running:
        flavour = OutlookFlavour.NEW
        problems.append("NEW_OUTLOOK_RUNNING_WITHOUT_CLASSIC")
    else:
        flavour = OutlookFlavour.CLASSIC
    if flavour == OutlookFlavour.CLASSIC and classic_running is False:
        notes.append("classic Outlook is installed but not running; start it in the signed-in session")
    if build is None and cur_ver is None:
        notes.append("Outlook build could not be read from the registry")
    return CompatibilityReport(
        checked_at=now,
        platform=plat,
        windows=True,
        progid_registered=registered,
        progid_clsid=clsid,
        progid_current_version=cur_ver,
        office_build=build,
        new_outlook_toggle_on=toggle_on,
        classic_process_running=classic_running,
        new_outlook_process_running=new_running,
        com_version=com_version,
        flavour=flavour,
        supported=flavour == OutlookFlavour.CLASSIC and not problems,
        problems=tuple(problems),
        notes=tuple(notes),
    )


def running_process_names() -> frozenset[str] | None:  # pragma: no cover - requires Windows
    """Running executable names via ``tasklist`` (read-only); ``None`` when unavailable."""
    import subprocess

    try:
        output = subprocess.run(
            ["tasklist", "/fo", "csv", "/nh"],  # noqa: S607 - fixed system tool, no shell
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    names = set()
    for line in output.splitlines():
        first = line.split(",", 1)[0].strip().strip('"').lower()
        if first:
            names.add(first)
    return frozenset(names)


__all__ = [
    "C2R_CONFIGURATION_KEY",
    "NEW_OUTLOOK_PREFERENCES_KEY",
    "NEW_OUTLOOK_TOGGLE_VALUE",
    "OUTLOOK_PROGID",
    "CompatibilityReport",
    "OutlookFlavour",
    "ProcessProbe",
    "RegistryReader",
    "WinRegReader",
    "check_compatibility",
    "running_process_names",
]
