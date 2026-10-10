"""Offline `CrawlClient` serving saved fixture files (tests, local demos, e2e).

It never touches the network. Each fixture directory contains a `MANIFEST.yaml`:

```yaml
source_key: fixture_dealer_de
designation: synthetic            # synthetic | real (real captures need retention permission)
created: 2026-10-06
parser_version: schemaorg_dealer@1.1.0
description: ...
files:                            # every fixture file with a description
  detail_normal.html: "..."
routes:                           # requested URL -> simulated response
  "https://dealer.example/fahrzeug/TEST-204":
    file: detail_normal.html
    status: 200                   # default 200
    final_url: null               # set to simulate a redirect (e.g. to another host)
    headers: {retry-after: "120"} # allow-listed response headers only
    simulate: none                # none | timeout | connection_error
    description: "..."
```

The same structural URL safety checks as a real client are applied (via
`suv_deals.netguard`) to the requested URL and to a simulated redirect target, so
fixtures cannot exercise URLs or redirect hops a real client would refuse.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from suv_deals.adapters.base import FetchOutcome, FetchPurpose, RawDocument
from suv_deals.clock import Clock, SystemClock
from suv_deals.domain.enums import AccessState
from suv_deals.errors import ValidationFailed
from suv_deals.netguard import UnsafeDestination, parse_safe_url

CRAWLER_VERSION = "fixture-client@1.0.0"
_ALLOWED_HEADERS = frozenset({"content-type", "retry-after", "last-modified", "etag", "x-robots-tag"})
DEFAULT_MAX_BYTES = 8_000_000


class FixtureRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    file: str | None = None
    status: int = Field(default=200, ge=100, le=599)
    final_url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    simulate: Literal["none", "timeout", "connection_error"] = "none"
    content_type: str = "text/html; charset=utf-8"
    description: str = Field(min_length=3, max_length=500)

    @field_validator("headers")
    @classmethod
    def _allow_listed(cls, value: dict[str, str]) -> dict[str, str]:
        lowered = {k.lower(): v for k, v in value.items()}
        unknown = set(lowered) - _ALLOWED_HEADERS
        if unknown:
            raise ValueError(f"headers not on the allow-list: {sorted(unknown)}")
        return lowered

    @model_validator(mode="after")
    def _shape(self) -> FixtureRoute:
        if self.simulate == "none" and self.file is None and 200 <= self.status < 300:
            raise ValueError("a successful route needs a file")
        return self


class FixtureManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(pattern=r"^[a-z0-9_]{3,60}$")
    designation: Literal["synthetic", "real"]
    created: date
    parser_version: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=3, max_length=2000)
    files: dict[str, str]
    routes: dict[str, FixtureRoute]

    @model_validator(mode="after")
    def _files_described(self) -> FixtureManifest:
        for url, route in self.routes.items():
            if route.file is not None and route.file not in self.files:
                raise ValueError(f"route {url} uses undescribed file {route.file}")
        return self


@dataclass(frozen=True, slots=True)
class FixtureRequest:
    url: str
    purpose: FetchPurpose
    source_key: str


@dataclass(frozen=True, slots=True)
class _Loaded:
    directory: Path
    manifest: FixtureManifest


def load_manifest(directory: Path) -> FixtureManifest:
    path = directory / "MANIFEST.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ValidationFailed(f"fixture directory {directory.name} has no MANIFEST.yaml") from None
    try:
        return FixtureManifest.model_validate(data)
    except ValueError as exc:
        raise ValidationFailed(f"invalid fixture manifest in {directory.name}: {exc}") from None


def _retry_after_seconds(value: str | None, now: datetime) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return int(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        return None
    return max(0, int((when - now).total_seconds()))


def _status_access(status: int) -> AccessState:
    if 200 <= status < 300:
        return AccessState.OK
    if status in (401, 403, 407):
        return AccessState.ACCESS_BLOCKED
    if status == 429:
        return AccessState.RATE_LIMITED
    if status == 404:
        return AccessState.NOT_FOUND
    if status == 410:
        return AccessState.REMOVED
    if status >= 500:
        return AccessState.TRANSIENT_ERROR
    return AccessState.UNEXPECTED_CONTENT


class FixtureCrawlClient:
    """`CrawlClient` over one or more fixture directories. Records every request in `requests`."""

    def __init__(
        self,
        directories: Iterable[Path],
        *,
        clock: Clock | None = None,
        max_response_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self._clock: Clock = clock or SystemClock()
        self._max_bytes = max_response_bytes
        self._sources: dict[str, _Loaded] = {}
        for directory in directories:
            manifest = load_manifest(directory)
            if manifest.source_key in self._sources:
                raise ValidationFailed(f"duplicate fixture source {manifest.source_key}")
            self._sources[manifest.source_key] = _Loaded(directory=directory.resolve(), manifest=manifest)
        self.requests: list[FixtureRequest] = []

    def manifest(self, source_key: str) -> FixtureManifest:
        return self._sources[source_key].manifest

    async def fetch(self, url: str, *, purpose: FetchPurpose, source_key: str) -> RawDocument:
        self.requests.append(FixtureRequest(url=url, purpose=purpose, source_key=source_key))
        now = self._clock.now()
        try:
            parse_safe_url(url)
        except UnsafeDestination as exc:
            return self._failure(url, now, AccessState.POLICY_DENIED, "POLICY_DENIED", str(exc))
        loaded = self._sources.get(source_key)
        route = loaded.manifest.routes.get(url) if loaded is not None else None
        if loaded is None or route is None:
            return self._failure(
                url, now, AccessState.NOT_FOUND, "FIXTURE_NOT_FOUND", "no fixture route", status=404
            )
        if route.final_url is not None and route.final_url != url:
            # A real client validates every redirect hop before following it; so does this one.
            try:
                parse_safe_url(route.final_url)
            except UnsafeDestination as exc:
                return self._failure(
                    url, now, AccessState.POLICY_DENIED, "REDIRECT_POLICY_DENIED", f"redirect refused: {exc}"
                )
        if route.simulate == "timeout":
            return self._failure(url, now, AccessState.TRANSIENT_ERROR, "TIMEOUT", "simulated timeout")
        if route.simulate == "connection_error":
            return self._failure(
                url, now, AccessState.TRANSIENT_ERROR, "CONNECTION_ERROR", "simulated failure"
            )
        body: bytes = b""
        if route.file is not None:
            path = (loaded.directory / route.file).resolve()
            if not path.is_relative_to(loaded.directory):
                raise ValidationFailed("fixture file escapes its directory")
            if not path.is_file():
                raise ValidationFailed(f"fixture file {route.file} is missing from {loaded.directory.name}")
            body = path.read_bytes()
        if len(body) > self._max_bytes:
            return self._failure(
                url,
                now,
                AccessState.UNEXPECTED_CONTENT,
                "RESPONSE_TOO_LARGE",
                "response exceeds byte cap",
                status=route.status,
            )
        headers = {"content-type": route.content_type, **route.headers}
        access = _status_access(route.status)
        final_url = route.final_url or url
        fetch = FetchOutcome(
            requested_url=url,
            final_url=final_url,
            http_status=route.status,
            success=access == AccessState.OK,
            access_state=access,
            error_code=None if access == AccessState.OK else f"HTTP_{route.status}",
            elapsed_ms=0,
            extraction_ms=0,
            bytes=len(body),
            redirect_count=1 if route.final_url and route.final_url != url else 0,
            retry_after_seconds=_retry_after_seconds(headers.get("retry-after"), now),
            response_headers=headers,
            crawler_version=CRAWLER_VERSION,
            fetched_at=now,
        )
        html = body.decode("utf-8", errors="replace") if body else None
        return RawDocument(
            url=url,
            final_url=final_url,
            fetched_at=now,
            content_type=route.content_type,
            html=html,
            raw_content_hash=hashlib.sha256(body).hexdigest() if body else None,
            fetch=fetch,
        )

    @staticmethod
    def _failure(
        url: str,
        now: datetime,
        access: AccessState,
        code: str,
        message: str,
        *,
        status: int | None = None,
    ) -> RawDocument:
        return RawDocument(
            url=url[:2048],
            final_url=None,
            fetched_at=now,
            fetch=FetchOutcome(
                requested_url=url[:2048],
                http_status=status,
                success=False,
                access_state=access,
                error_code=code,
                error_message=message[:500],
                crawler_version=CRAWLER_VERSION,
                fetched_at=now,
            ),
        )
