"""Classic vs new Outlook detection (spec 37.6 "Outlook compatibility and execution"; U7)."""

from __future__ import annotations

import inspect

from bridge_support import START, FakeRegistry, classic_registry
from outlook_bridge import compatibility
from outlook_bridge.compatibility import (
    NEW_OUTLOOK_PREFERENCES_KEY,
    NEW_OUTLOOK_TOGGLE_VALUE,
    OutlookFlavour,
    check_compatibility,
)


def test_classic_outlook_is_supported() -> None:
    report = check_compatibility(
        now=START,
        platform="win32",
        registry=classic_registry(),
        processes=lambda: frozenset({"outlook.exe"}),
    )
    assert report.flavour == OutlookFlavour.CLASSIC
    assert report.supported is True
    assert report.problems == ()
    assert report.progid_registered is True
    assert report.office_build == "16.0.17928.20114"
    assert report.progid_current_version == "Outlook.Application.16"
    assert report.report_flavour == "classic"
    assert report.settings_modified is False


def test_new_outlook_toggle_is_reported_unsupported() -> None:
    report = check_compatibility(
        now=START,
        platform="win32",
        registry=classic_registry(toggle=1),
        processes=lambda: frozenset({"olk.exe"}),
    )
    assert report.flavour == OutlookFlavour.NEW
    assert report.supported is False
    assert "NEW_OUTLOOK_ENABLED" in report.problems
    assert report.report_flavour == "new"
    assert any("nothing was changed" in note for note in report.notes)
    assert report.settings_modified is False


def test_new_outlook_toggle_as_string_value() -> None:
    report = check_compatibility(now=START, platform="win32", registry=classic_registry(toggle=0))
    assert report.supported is True
    registry = classic_registry()
    registry.values[("HKCU", NEW_OUTLOOK_PREFERENCES_KEY, NEW_OUTLOOK_TOGGLE_VALUE)] = "1"
    assert check_compatibility(now=START, platform="win32", registry=registry).flavour == OutlookFlavour.NEW


def test_only_new_outlook_installed_is_unsupported() -> None:
    report = check_compatibility(
        now=START,
        platform="win32",
        registry=FakeRegistry(),
        processes=lambda: frozenset({"olk.exe"}),
    )
    assert report.flavour == OutlookFlavour.NEW
    assert report.supported is False
    assert {"OUTLOOK_COM_NOT_REGISTERED", "NEW_OUTLOOK_UNSUPPORTED"} <= set(report.problems)


def test_new_outlook_running_without_classic_is_unsupported() -> None:
    report = check_compatibility(
        now=START,
        platform="win32",
        registry=classic_registry(),
        processes=lambda: frozenset({"olk.exe"}),
    )
    assert report.flavour == OutlookFlavour.NEW
    assert "NEW_OUTLOOK_RUNNING_WITHOUT_CLASSIC" in report.problems
    assert report.supported is False


def test_nothing_installed() -> None:
    report = check_compatibility(now=START, platform="win32", registry=FakeRegistry(), processes=lambda: None)
    assert report.flavour == OutlookFlavour.NOT_INSTALLED
    assert report.supported is False
    assert report.classic_process_running is None


def test_not_windows_is_unsupported_without_touching_the_registry() -> None:
    registry = classic_registry()
    report = check_compatibility(now=START, platform="linux", registry=registry)
    assert report.supported is False
    assert report.windows is False
    assert report.problems == ("NOT_WINDOWS",)
    assert registry.reads == []


def test_classic_installed_but_not_running_is_a_note_not_a_problem() -> None:
    report = check_compatibility(
        now=START,
        platform="win32",
        registry=classic_registry(),
        processes=lambda: frozenset({"explorer.exe"}),
    )
    assert report.supported is True
    assert any("not running" in note for note in report.notes)


def test_com_version_probe_failure_is_a_note() -> None:
    def failing() -> str | None:
        raise RuntimeError("Outlook not running")

    report = check_compatibility(
        now=START, platform="win32", registry=classic_registry(), com_version_probe=failing
    )
    assert report.supported is True
    assert report.com_version is None
    assert any("COM probe failed" in note for note in report.notes)
    ok = check_compatibility(
        now=START, platform="win32", registry=classic_registry(), com_version_probe=lambda: "16.0.1"
    )
    assert ok.com_version == "16.0.1"


def test_registry_interface_is_read_only_by_construction() -> None:
    """The check never changes Outlook, Trust Center, registry or antivirus settings."""
    methods = {name for name, _ in inspect.getmembers(compatibility.WinRegReader, inspect.isfunction)}
    assert methods == {"__init__", "read_value"}
    source = inspect.getsource(compatibility)
    for forbidden in ("SetValue", "CreateKey", "DeleteKey", "DeleteValue", "KEY_WRITE", "KEY_ALL_ACCESS"):
        assert forbidden not in source
