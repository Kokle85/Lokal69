"""Synthetic data builders and role helpers for the database integration tests.

Everything created here is SYNTHETIC test data: names, URLs and identifiers are
visibly labelled and use reserved example domains. No real listing, vehicle,
person, tax rate or credential appears in these tests.

Tests connect as the superuser test role (which bypasses RLS) to arrange data,
and use :func:`as_role` / :func:`backend` to act as ``suv_backend``, ``anon`` or
``authenticated`` inside a transaction with transaction-local GUCs, exactly as
the repository layer does (``set_config(name, value, true)``).
"""

from __future__ import annotations

import hashlib
import itertools
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

SYNTHETIC_DOMAIN = "synthetic-dealer.example"
T0 = datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)

# SQLSTATEs raised by the guard functions in supabase/migrations (docs/schema.md).
SV_APPEND_ONLY = "SV001"
SV_TRANSITION = "SV002"
SV_REFERENCE = "SV003"
SV_FROZEN = "SV004"
SV_MONOTONIC = "SV005"
SV_GENERATION = "SV006"

_counter = itertools.count(1)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unique(prefix: str) -> str:
    return f"{prefix}_{next(_counter)}_{uuid.uuid4().hex[:8]}"


@contextmanager
def as_role(
    conn: psycopg.Connection,
    role: str,
    *,
    workspace_id: UUID | str | None = None,
    user_id: UUID | str | None = None,
    credential_hash: str | None = None,
) -> Iterator[psycopg.Connection]:
    """Run the block in one transaction as ``role`` with transaction-local GUCs."""
    with conn.transaction():
        conn.execute(sql.SQL("set local role {}").format(sql.Identifier(role)))
        for name, value in (
            ("app.workspace_id", workspace_id),
            ("app.user_id", user_id),
            ("app.credential_hash", credential_hash),
        ):
            if value is not None:
                conn.execute("select set_config(%s, %s, true)", (name, str(value)))
        yield conn


def backend(
    conn: psycopg.Connection,
    workspace_id: UUID | str | None = None,
    **kwargs: Any,
) -> Any:
    return as_role(conn, "suv_backend", workspace_id=workspace_id, **kwargs)


def table_ident(qualified: str) -> sql.Identifier:
    schema, table = qualified.split(".", 1)
    return sql.Identifier(schema, table)


class Seed:
    """Inserts synthetic rows as the superuser test role (RLS bypassed, triggers active)."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def insert(self, table: str, returning: str = "id", **cols: Any) -> Any:
        names = list(cols)
        query = sql.SQL("insert into {} ({}) values ({}) returning {}").format(
            table_ident(table),
            sql.SQL(", ").join(sql.Identifier(n) for n in names),
            sql.SQL(", ").join(sql.Placeholder() for _ in names),
            sql.Identifier(returning),
        )
        values = [Jsonb(v) if isinstance(v, dict) else v for v in cols.values()]
        row = self.conn.execute(query, values).fetchone()
        assert row is not None
        return row[0]

    def insert_id(self, table: str, **cols: Any) -> UUID:
        value = self.insert(table, **cols)
        assert isinstance(value, UUID)
        return value

    def scalar(self, query: str, params: tuple[Any, ...] | dict[str, Any] | None = None) -> Any:
        row = self.conn.execute(query, params).fetchone()
        assert row is not None
        return row[0]

    # --- core ---------------------------------------------------------------------------
    def workspace(self, name: str = "Synthetic workspace", *, active: bool = True) -> UUID:
        return self.insert_id("app.workspaces", name=f"{name} (synthetic)", active=active)

    def user(self) -> UUID:
        user_id = uuid.uuid4()
        self.insert("auth.users", id=user_id, email=f"{unique('user')}@example.invalid")
        return user_id

    def membership(
        self, workspace_id: UUID, user_id: UUID, role: str = "owner", *, active: bool = True
    ) -> None:
        self.insert(
            "app.memberships",
            returning="user_id",
            workspace_id=workspace_id,
            user_id=user_id,
            role=role,
            active=active,
        )

    def config_revision(self, workspace_id: UUID) -> UUID:
        revision = self.scalar(
            "select coalesce(max(revision), 0) + 1 from app.config_revisions where workspace_id = %s",
            (workspace_id,),
        )
        config = {"synthetic": True, "revision": revision}
        return self.insert_id(
            "app.config_revisions",
            workspace_id=workspace_id,
            revision=revision,
            config=config,
            config_hash=sha(f"config-{workspace_id}-{revision}"),
            author_principal_id=uuid.uuid4(),
            author_kind="system",
            reason="synthetic test configuration",
        )

    def source(self, workspace_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "source_key": unique("fixture_src").lower(),
            "display_name": "Synthetic fixture dealer",
            "country": "DE",
            "role": "acquisition",
            "mode": "fixture",
            "adapter": "fixture_adapter",
        }
        values.update(cols)
        return self.insert_id("app.sources", **values)

    def profile(
        self, workspace_id: UUID, key: str = "primary", config_revision_id: UUID | None = None
    ) -> UUID:
        cfg = config_revision_id or self.config_revision(workspace_id)
        shapes: dict[str, dict[str, Any]] = {
            "primary": {
                "min_price_eur": "2500.00",
                "max_price_eur": "3000.00",
                "max_price_inclusive": True,
                "enabled": True,
                "queue_label": "Primary queue",
            },
            "manual_4000": {
                "min_price_eur": "2500.00",
                "max_price_eur": "4000.00",
                "max_price_inclusive": True,
                "enabled": False,
                "queue_label": "Manual EUR 4,000 review queue",
            },
            "below_target_watch": {
                "min_price_eur": None,
                "max_price_eur": "2500.00",
                "max_price_inclusive": False,
                "enabled": False,
                "queue_label": "Below-target watch queue",
            },
        }
        return self.insert_id(
            "app.search_profiles",
            workspace_id=workspace_id,
            profile_key=key,
            label=f"Synthetic {key} profile",
            config_revision_id=cfg,
            **shapes[key],
        )

    # --- listings -----------------------------------------------------------------------
    def listing(self, workspace_id: UUID, source_id: UUID, **cols: Any) -> UUID:
        slid = cols.pop("source_listing_id", unique("TEST"))
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "source_id": source_id,
            "source_listing_id": slid,
            "canonical_url": f"https://{SYNTHETIC_DOMAIN}/vehicles/{slid}",
            "identity_method": "provider_id",
            "identity_material": f"fixture:{slid}",
            "identity_hash": sha(f"{source_id}:{slid}"),
            "identity_confidence": "high",
            "first_seen_at": T0,
            "last_seen_at": T0,
        }
        values.update(cols)
        return self.insert_id("app.listings", **values)

    def allocate_generation(self, workspace_id: UUID, listing_id: UUID) -> int:
        return int(
            self.scalar(
                "update app.listings set detail_generation = detail_generation + 1"
                " where workspace_id = %s and id = %s returning detail_generation",
                (workspace_id, listing_id),
            )
        )

    def detail_observation(
        self,
        workspace_id: UUID,
        listing_id: UUID,
        *,
        generation: int | None = None,
        semantic_hash: str | None = None,
        promoted: bool = False,
    ) -> tuple[UUID, int, UUID]:
        gen = generation if generation is not None else self.allocate_generation(workspace_id, listing_id)
        observation_id = uuid.uuid4()
        row_id = self.insert(
            "app.detail_observations",
            workspace_id=workspace_id,
            listing_id=listing_id,
            generation=gen,
            observation_id=observation_id,
            semantic_hash=semantic_hash or sha(unique("semantic")),
            normalized={"synthetic": True},
            provenance={"price.amount_minor": {"method": "css", "synthetic": True}},
            parser_version="fixture_dealer_de@1.0.0",
            observed_at=T0,
            promoted=promoted,
        )
        return row_id, gen, observation_id

    def revision(self, workspace_id: UUID, listing_id: UUID, number: int, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "listing_id": listing_id,
            "revision_number": number,
            "observed_at": T0 + timedelta(minutes=number),
            "semantic_hash": sha(unique("semantic")),
            "asking_minor": 275000,
            "currency": "EUR",
            "price_basis": "gross",
            "price_type": "full_vehicle_asking",
            "mileage_km": "187500",
            "normalized": {"synthetic": True, "revision": number},
            "provenance": {"synthetic": True},
            "parser_version": "fixture_dealer_de@1.0.0",
        }
        values.update(cols)
        return self.insert_id("app.listing_revisions", **values)

    def promote(
        self, workspace_id: UUID, listing_id: UUID, revision_id: UUID, generation: int, observation_id: UUID
    ) -> None:
        self.conn.execute(
            "update app.listings set current_revision_id = %s, current_generation = %s,"
            " current_observation_id = %s, row_version = row_version + 1"
            " where workspace_id = %s and id = %s",
            (revision_id, generation, observation_id, workspace_id, listing_id),
        )

    def snapshot(self, workspace_id: UUID, source_id: UUID) -> UUID:
        return self.insert_id(
            "ops.source_snapshots",
            workspace_id=workspace_id,
            source_id=source_id,
            url_hash=sha(unique("url")),
            content_hash=sha(unique("content")),
            mime_type="text/html",
            bytes=1234,
            fetched_at=T0,
            storage_backend="disabled",
            retention_policy="hash_only",
        )

    def crawl_run(self, workspace_id: UUID, source_id: UUID) -> UUID:
        return self.insert_id(
            "ops.crawl_runs",
            workspace_id=workspace_id,
            source_id=source_id,
            adapter_version="fixture@1.0.0",
            coverage_mode="rolling_pages",
        )

    # --- queue / outbox -----------------------------------------------------------------
    def job(self, workspace_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "job_type": "valuation",
            "dedup_key": unique("job"),
        }
        values.update(cols)
        return self.insert_id("ops.jobs", **values)

    def outbox(self, workspace_id: UUID, **cols: Any) -> UUID:
        payload = cols.pop("payload", {"schema_version": "1.0", "synthetic": True})
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "event_type": "review.pending",
            "aggregate_type": "review_case",
            "aggregate_id": uuid.uuid4(),
            "payload": payload,
            "payload_hash": sha(str(payload)),
            "dedup_key": unique("review.pending"),
        }
        values.update(cols)
        return self.insert_id("ops.outbox", **values)

    # --- reviews ------------------------------------------------------------------------
    def review_case(self, workspace_id: UUID, listing_id: UUID, revision_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "listing_id": listing_id,
            "revision_id": revision_id,
            "profile_key": "primary",
            "queue_label": "Primary queue",
            "readiness": "needs_import_costs",
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.review_cases", **values)

    def decision(
        self, workspace_id: UUID, case_id: UUID, listing_id: UUID, revision_id: UUID, **cols: Any
    ) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "case_id": case_id,
            "case_version": 1,
            "listing_id": listing_id,
            "listing_revision_id": revision_id,
            "actor_principal_id": uuid.uuid4(),
            "actor_kind": "mcp_client",
            "actor_role": "reviewer",
            "outcome": "watch",
            "reason_codes": ["SYNTHETIC_TEST"],
            "summary": "Synthetic test decision; not a real vehicle.",
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.review_decisions", **values)

    # --- market, tax, costs, valuations -------------------------------------------------
    def fx_rate(self, workspace_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "base": "EUR",
            "quote": "CHF",
            "rate": "0.9312000000",
            "rate_date": T0.date(),
            "retrieved_at": T0,
            "provider": unique("synthetic_fx"),
            "purpose": "reference",
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.fx_rates", **values)

    def tax_rule(self, workspace_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "rule_set_id": unique("mk-passenger-import-UNAPPROVED-example"),
            "jurisdiction": "MK",
            "version": "draft-1",
            "status": "unapproved",
            "rules": {"components": [], "synthetic": True},
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.tax_rule_sets", **values)

    def approved_tax_rule(self, workspace_id: UUID, **cols: Any) -> UUID:
        """A structurally approved rule set with SYNTHETIC evidence (no real rates)."""
        values: dict[str, Any] = {
            "status": "approved",
            "is_fixture": False,
            "valid_from": T0.date(),
            "currency": "MKD",
            "sources": Jsonb([{"synthetic": "https://tax-authority.example/rules.pdf"}]),
            "sha256": sha(unique("rules")),
            "approved_by": uuid.uuid4(),
            "approved_at": T0,
            "approval_reference": "synthetic approval record",
        }
        values.update(cols)
        return self.tax_rule(workspace_id, **values)

    def cost_evidence(self, workspace_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "kind": "estimate",
            "category": "transport",
            "base_minor": 70000,
            "currency": "EUR",
            "obtained_at": T0,
            "scope": {"synthetic": True, "origin_country": "DE"},
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.cost_evidence", **values)

    def market_observation(self, workspace_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "evidence_kind": "asking_price",
            "amount_minor": 900000,
            "currency": "EUR",
            "normalized": {"synthetic": True},
            "make": "Example",
            "model": "Trail",
            "observed_at": T0,
            "url": "https://mk-classifieds.example/ad/SYNTHETIC-1",
            "confidence": "medium",
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.market_observations", **values)

    def comparable_set(self, workspace_id: UUID, listing_id: UUID, revision_id: UUID, **cols: Any) -> UUID:
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "listing_id": listing_id,
            "target_revision_id": revision_id,
            "criteria_version": "synthetic-1",
            "criteria": {"synthetic": True},
            "sample_size": 0,
            "selected_count": 0,
            "excluded_count": 0,
            "sample_quality": "insufficient",
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.comparable_sets", **values)

    def valuation(self, workspace_id: UUID, listing_id: UUID, revision_id: UUID, **cols: Any) -> UUID:
        cfg = cols.pop("config_revision_id", None) or self.config_revision(workspace_id)
        values: dict[str, Any] = {
            "workspace_id": workspace_id,
            "listing_id": listing_id,
            "listing_revision_id": revision_id,
            "config_revision_id": cfg,
            "dependency_fingerprint": sha(unique("deps")),
            "state": "incomplete",
            "calculation_version": "synthetic-calc-1",
            "unknowns": Jsonb(["import_components"]),
            "is_fixture": True,
        }
        values.update(cols)
        return self.insert_id("app.valuations", **values)


@dataclass(frozen=True)
class World:
    """One synthetic workspace with a source, primary profile, listing and first revision."""

    workspace_id: UUID
    config_revision_id: UUID
    source_id: UUID
    profile_id: UUID
    listing_id: UUID
    generation: int
    observation_id: UUID
    revision_id: UUID


def build_world(seed: Seed, name: str) -> World:
    ws = seed.workspace(name)
    cfg = seed.config_revision(ws)
    src = seed.source(ws)
    prof = seed.profile(ws, "primary", cfg)
    listing = seed.listing(ws, src)
    _, gen, obs = seed.detail_observation(ws, listing, promoted=True)
    rev = seed.revision(ws, listing, 1, detail_generation=gen, observation_id=obs)
    seed.promote(ws, listing, rev, gen, obs)
    return World(ws, cfg, src, prof, listing, gen, obs, rev)
