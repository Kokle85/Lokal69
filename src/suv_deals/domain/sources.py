"""Source registry configuration (spec section 5).

Terms decisions and technical status are recorded independently. A stored
owner acknowledgement audits a decision; it is not legal permission.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from suv_deals.domain.enums import SourceMode, TechnicalStatus, TermsDecision, TermsStatus


class RateBudget(BaseModel):
    """Engineering defaults, not provider-approved quotas."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_concurrency_per_host: int = Field(default=1, ge=1, le=1)
    min_delay_seconds: int = Field(default=20, ge=5, le=3600)
    max_search_pages_per_run: int = Field(default=2, ge=1, le=20)
    max_detail_jobs_per_run: int = Field(default=10, ge=0, le=200)
    daily_request_budget: int = Field(default=200, ge=0, le=20000)
    daily_byte_budget: int = Field(default=200_000_000, ge=0)
    request_timeout_seconds: int = Field(default=45, ge=5, le=180)
    max_response_bytes: int = Field(default=8_000_000, ge=10_000, le=50_000_000)
    max_redirects: int = Field(default=3, ge=0, le=5)


class SourceConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source_key: str = Field(pattern=r"^[a-z0-9_]{3,60}$")
    display_name: str = Field(min_length=2, max_length=120)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    role: str = Field(pattern=r"^(acquisition|mk_comparable)$")
    mode: SourceMode
    adapter: str = Field(min_length=3, max_length=80)  # registry key of the adapter class
    adapter_version: str = Field(min_length=1, max_length=40)
    enabled: bool = False
    technical_status: TechnicalStatus = TechnicalStatus.UNTESTED
    terms_status: TermsStatus = TermsStatus.UNREVIEWED
    terms_url: str | None = None
    terms_reviewed_at: datetime | None = None
    terms_decision: TermsDecision = TermsDecision.PENDING
    terms_decision_actor: str | None = None
    terms_decision_note: str | None = Field(default=None, max_length=2000)
    technical_denial_policy: str = Field(default="stop_and_report", pattern=r"^stop_and_report$")
    robots_policy: str = Field(default="obey", pattern=r"^obey$")
    allowed_hosts: tuple[str, ...] = ()
    allowed_search_paths: tuple[str, ...] = ()  # regex patterns on path (anchored)
    allowed_detail_paths: tuple[str, ...] = ()
    tracking_params: tuple[str, ...] = (
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "gclid",
        "fbclid",
        "ref",
        "referrer",
    )
    source_timezone: str = "Europe/Berlin"
    rate_budget: RateBudget = RateBudget()
    search: dict[str, str] = Field(default_factory=dict)  # adapter-specific, documented per source
    notes: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _activation_guard(self) -> SourceConfig:
        if self.enabled:
            problems = activation_problems(self)
            if problems:
                raise ValueError(f"source {self.source_key} cannot be enabled: {'; '.join(problems)}")
        return self


def activation_problems(cfg: SourceConfig) -> list[str]:
    """Reasons a source may not run network work. Empty list means the gate is satisfied."""
    problems: list[str] = []
    if cfg.adapter_version == "unimplemented":
        problems.append("adapter is unimplemented")
    if cfg.terms_decision in (TermsDecision.PENDING, TermsDecision.DO_NOT_USE):
        problems.append(f"terms decision is {cfg.terms_decision.value}")
    if cfg.terms_status == TermsStatus.UNREVIEWED:
        problems.append("terms not reviewed")
    if cfg.terms_decision != TermsDecision.PENDING and not cfg.terms_decision_actor:
        problems.append("terms decision has no recorded actor")
    if cfg.technical_status in (TechnicalStatus.ACCESS_BLOCKED, TechnicalStatus.PARSER_UNHEALTHY):
        problems.append(f"technical status is {cfg.technical_status.value}")
    if cfg.technical_status == TechnicalStatus.UNTESTED:
        problems.append("adapter has not passed fixture tests")
    if not cfg.allowed_hosts:
        problems.append("no allowed hosts")
    if not cfg.allowed_search_paths and cfg.role == "acquisition":
        problems.append("no allowed search paths")
    if not cfg.allowed_detail_paths:
        problems.append("no allowed detail paths")
    return problems


def load_source_configs(sources_dir: Path) -> list[SourceConfig]:
    configs: list[SourceConfig] = []
    for path in sorted(sources_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        configs.append(SourceConfig.model_validate(data))
    keys = [c.source_key for c in configs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate source_key in config/sources")
    return configs
