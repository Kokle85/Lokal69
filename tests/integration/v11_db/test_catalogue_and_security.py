"""Catalogue, RLS, grants and scope checks for migration 20261006001000 (spec 11, 12, 37.8; ADR 0001).

- Every new table: workspace-owned, ``unique (workspace_id, id)``, RLS enabled, the standard
  ``tenant_isolation`` policy for ``suv_backend`` only, ``created_at timestamptz``.
- ``anon``/``authenticated``/``service_role``/PUBLIC have nothing; ``suv_backend`` has exactly the
  documented privileges (no DELETE/TRUNCATE anywhere, append-only tables are SELECT/INSERT,
  identity/content columns not updatable); ``backend_role_problems`` stays empty.
- Workspace isolation holds for every new table.
- API credential scope checks mirror ``domain.actor.ROLE_SCOPES`` plus the mail:ingest-only rule.
- The SQL helpers agree with the Python domain (identity key, message body hash).
"""

from __future__ import annotations

import itertools
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql
from tests.integration.db.helpers import T0, Seed, as_role, backend, sha, table_ident
from tests.integration.v11_db.support import (
    APPEND_ONLY_V11,
    V11_TABLES,
    InquiryWorld,
    audit_event,
    expect_sqlstate,
    insert_reply,
    insert_row,
    mailbox,
    publish_binding,
    rendered,
    reply_values,
    sent_inquiry,
    suppression,
)

from suv_deals.domain.actor import ROLE_SCOPES
from suv_deals.domain.enums import Role, Scope
from suv_deals.domain.seller_templates import TEMPLATES, message_body_hash

pytestmark = pytest.mark.db

CLIENT_ROLES = ("anon", "authenticated", "service_role")


# ---------------------------------------------------------------------------------------------
# Catalogue shape
# ---------------------------------------------------------------------------------------------


def test_new_tables_exist_with_workspace_shape_rls_and_tenant_policy(db_conn: psycopg.Connection) -> None:
    for table in V11_TABLES:
        row = db_conn.execute(
            "select c.oid, c.relrowsecurity, a.attnotnull from pg_class c"
            " join pg_attribute a on a.attrelid = c.oid and a.attname = 'workspace_id' and not a.attisdropped"
            " where c.oid = %s::regclass",
            (table,),
        ).fetchone()
        assert row is not None, table
        oid, rls, not_null = row
        assert rls and not_null, table
        policy = db_conn.execute(
            "select pg_get_expr(p.polqual, p.polrelid), pg_get_expr(p.polwithcheck, p.polrelid),"
            " array(select rolname from pg_roles where oid = any(p.polroles)), p.polcmd"
            " from pg_policy p where p.polrelid = %s and p.polname = 'tenant_isolation'",
            (oid,),
        ).fetchone()
        assert policy is not None, table
        using, check, roles, cmd = policy
        assert "workspace_id = ( SELECT app.current_workspace_id()" in using, table
        assert "workspace_id = ( SELECT app.current_workspace_id()" in check, table
        assert roles == ["suv_backend"] and cmd == "*", table
        assert db_conn.execute("select count(*) from pg_policy where polrelid = %s", (oid,)).fetchone() == (
            1,
        ), f"{table} has unexpected extra policies"
        created = db_conn.execute(
            "select format_type(atttypid, atttypmod), attnotnull from pg_attribute"
            " where attrelid = %s and attname = 'created_at' and not attisdropped",
            (oid,),
        ).fetchone()
        assert created == ("timestamp with time zone", True), table
        composite = db_conn.execute(
            "select count(*) from pg_constraint k where k.conrelid = %s and k.contype in ('u', 'p')"
            " and (select array_agg(attname::text order by attname) from pg_attribute"
            "      where attrelid = k.conrelid and attnum = any(k.conkey)) = array['id', 'workspace_id']",
            (oid,),
        ).fetchone()
        assert composite == (1,), f"{table} lacks unique (workspace_id, id)"


def test_cross_table_links_are_composite_workspace_foreign_keys(db_conn: psycopg.Connection) -> None:
    rows = db_conn.execute(
        "select conrelid::regclass::text, conname, pg_get_constraintdef(oid) from pg_constraint"
        " where contype = 'f' and conrelid = any(%s::regclass[])",
        (list(V11_TABLES),),
    ).fetchall()
    assert rows
    for table, name, definition in rows:
        if "REFERENCES app.workspaces(id)" in definition:
            continue
        assert definition.startswith("FOREIGN KEY (workspace_id, "), (table, name, definition)


def test_no_float_or_naive_timestamp_columns(db_conn: psycopg.Connection) -> None:
    rows = db_conn.execute(
        "select table_schema || '.' || table_name || '.' || column_name from information_schema.columns"
        " where table_schema || '.' || table_name = any(%s)"
        " and data_type in ('timestamp without time zone', 'real', 'double precision', 'money')",
        (list(V11_TABLES),),
    ).fetchall()
    assert rows == []


@pytest.mark.parametrize(
    ("index", "fragments"),
    [
        ("app.seller_inquiries_dispatch_idx", ["(workspace_id, queued_at, id)", "'queued'"]),
        (
            "app.seller_inquiries_listing_seller_uidx",
            ["UNIQUE", "qualification_listing_id, seller_entity_id"],
        ),
        ("app.seller_inquiries_message_id_uidx", ["UNIQUE", "rfc_message_id"]),
        ("app.seller_inquiries_seller_idx", ["(workspace_id, seller_entity_id, reserved_at)"]),
        ("ops.inquiry_quota_ledger_window_idx", ["(workspace_id, debited_at)", "released_at IS NULL"]),
        ("ops.inquiry_quota_ledger_active_uidx", ["UNIQUE", "(workspace_id, inquiry_id)"]),
        ("ops.email_suppressions_match_idx", ["(workspace_id, scope, match_key)", "removed_at IS NULL"]),
        ("ops.email_delivery_attempts_running_uidx", ["UNIQUE", "'running'"]),
        ("ops.email_delivery_attempts_lease_idx", ["(lease_expires_at)", "'running'"]),
        ("ops.mail_binding_sync_sequence_uk", ["UNIQUE", "(workspace_id, mailbox_binding_id, sequence)"]),
        (
            "app.seller_replies_message_uidx",
            ["UNIQUE", "(workspace_id, mailbox_binding_id, internet_message_id)"],
        ),
        ("app.seller_replies_fingerprint_uidx", ["UNIQUE", "source_fingerprint"]),
        ("ops.mail_worker_bindings_active_mailbox_uidx", ["UNIQUE", "'active'"]),
        ("app.seller_contacts_verified_uidx", ["UNIQUE", "(workspace_id, listing_id)", "'verified'"]),
        ("app.availability_events_listing_idx", ["(workspace_id, listing_id, effective_at DESC)"]),
    ],
)
def test_required_indexes(db_conn: psycopg.Connection, index: str, fragments: list[str]) -> None:
    row = db_conn.execute("select pg_get_indexdef(%s::regclass)", (index,)).fetchone()
    assert row is not None
    for fragment in fragments:
        assert fragment in row[0], f"{index}: {row[0]}"


def test_every_new_foreign_key_is_indexed(db_conn: psycopg.Connection) -> None:
    """Each FK's columns are a prefix of some index (no sequential scans on parent changes)."""
    fks = db_conn.execute(
        "select conrelid, conrelid::regclass::text, conname, conkey from pg_constraint"
        " where contype = 'f' and conrelid = any(%s::regclass[])",
        (list(V11_TABLES),),
    ).fetchall()
    missing = []
    for relid, table, name, conkey in fks:
        indexes = db_conn.execute(
            "select indkey::int2[] from pg_index where indrelid = %s", (relid,)
        ).fetchall()
        workspace_attnum = db_conn.execute(
            "select attnum from pg_attribute where attrelid = %s and attname = 'workspace_id'", (relid,)
        ).fetchone()
        assert workspace_attnum is not None
        if conkey == [workspace_attnum[0]]:
            continue  # the plain workspaces FK: every index leads with workspace_id
        covered = any(set(list(ix[0])[: len(conkey)]) == set(conkey) for ix in indexes)
        if not covered:
            missing.append(f"{table}.{name}")
    assert missing == []


# ---------------------------------------------------------------------------------------------
# Grants and client roles
# ---------------------------------------------------------------------------------------------


def test_client_roles_have_no_privileges_on_new_tables(db_conn: psycopg.Connection) -> None:
    for role, table in itertools.product(CLIENT_ROLES, V11_TABLES):
        row = db_conn.execute(
            "select has_table_privilege(%(r)s, %(t)s,"
            " 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')"
            " or has_any_column_privilege(%(r)s, %(t)s, 'SELECT,INSERT,UPDATE,REFERENCES')",
            {"r": role, "t": table},
        ).fetchone()
        assert row == (False,), (role, table)


@pytest.mark.parametrize("role", ["anon", "authenticated"])
def test_client_roles_are_denied_on_every_new_table(
    db_conn: psycopg.Connection, iw: InquiryWorld, role: str
) -> None:
    claims = f'{{"sub": "{uuid.uuid4()}", "role": "{role}"}}'
    for table in V11_TABLES:
        with (
            pytest.raises(errors.InsufficientPrivilege),
            as_role(db_conn, role, workspace_id=iw.workspace_id),
        ):
            db_conn.execute("select set_config('request.jwt.claims', %s, true)", (claims,))
            db_conn.execute(sql.SQL("select 1 from {} limit 1").format(table_ident(table)))


def test_new_routines_are_private_pinned_and_not_security_definer(db_conn: psycopg.Connection) -> None:
    rows = db_conn.execute(
        "select p.oid::regprocedure::text, p.prosecdef, p.proconfig, p.proacl is null,"
        " has_function_privilege('public', p.oid, 'EXECUTE'),"
        " has_function_privilege('anon', p.oid, 'EXECUTE'),"
        " has_function_privilege('authenticated', p.oid, 'EXECUTE')"
        " from pg_proc p join pg_namespace n on n.oid = p.pronamespace"
        " where n.nspname in ('app', 'ops') and (p.proname like 'seller%%' or p.proname like 'mail%%'"
        "   or p.proname like 'email%%' or p.proname like 'inquiry%%' or p.proname like 'availability%%'"
        "   or p.proname in ('rfc_message_id_ok', 'rfc_message_id_array_ok', 'opaque_ref_ok',"
        "                    'hex64_array_ok', 'sender_display_name_ok', 'message_body_hash',"
        "                    'reply_attachments_ok'))"
    ).fetchall()
    assert len(rows) >= 25
    for name, secdef, config, null_acl, public_exec, anon_exec, auth_exec in rows:
        assert not secdef, name
        assert config == ['search_path=""'], name
        assert not null_acl, name
        assert not (public_exec or anon_exec or auth_exec), name


def test_backend_privilege_matrix_on_new_tables(db_conn: psycopg.Connection) -> None:
    for table in V11_TABLES:
        row = db_conn.execute(
            "select has_table_privilege('suv_backend', %(t)s, 'SELECT'),"
            " has_table_privilege('suv_backend', %(t)s, 'INSERT'),"
            " has_any_column_privilege('suv_backend', %(t)s, 'UPDATE'),"
            " has_table_privilege('suv_backend', %(t)s, 'DELETE'),"
            " has_table_privilege('suv_backend', %(t)s, 'TRUNCATE'),"
            " has_table_privilege('suv_backend', %(t)s, 'REFERENCES,TRIGGER')",
            {"t": table},
        ).fetchone()
        assert row is not None
        select, insert, update, delete, truncate, ddlish = row
        assert select and insert, table
        assert not delete and not truncate and not ddlish, table
        assert update == (table not in APPEND_ONLY_V11), table


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("app.seller_inquiries", "identity_key"),
        ("app.seller_inquiries", "seller_entity_id"),
        ("app.seller_inquiries", "vehicle_cluster_id"),
        ("app.seller_inquiries", "workspace_id"),
        ("app.seller_replies", "sanitized_body"),
        ("app.seller_replies", "internet_message_id"),
        ("app.seller_replies", "source_fingerprint"),
        ("app.seller_contacts", "address"),
        ("app.seller_contacts", "listing_id"),
        ("ops.email_delivery_attempts", "attempt_id"),
        ("ops.email_delivery_attempts", "sender_binding_id"),
        ("ops.email_delivery_attempts", "lease_token"),
        ("ops.email_suppressions", "reason"),
        ("ops.email_suppressions", "scope_key"),
        ("ops.inquiry_quota_ledger", "debited_at"),
        ("ops.email_sender_bindings", "from_address"),
        ("ops.email_sender_bindings", "account_id"),
        ("ops.mail_worker_bindings", "sender_binding_id"),
        ("ops.mail_ingest_dedup", "dedup_key"),
        ("app.seller_entity_aliases", "alias_key_hash"),
        ("app.seller_inquiry_controls", "workspace_id"),
    ],
)
def test_backend_cannot_update_identity_or_content_columns(
    db_conn: psycopg.Connection, table: str, column: str
) -> None:
    assert db_conn.execute(
        "select has_column_privilege('suv_backend', %s, %s, 'UPDATE')", (table, column)
    ).fetchone() == (False,)


def test_backend_role_stays_safe_and_defaults_stay_private(db_conn: psycopg.Connection) -> None:
    assert db_conn.execute("select ops.backend_role_problems()").fetchone() == ([],)
    with db_conn.transaction(force_rollback=True):
        owner = db_conn.execute(
            "select pg_catalog.pg_get_userbyid(datdba) from pg_catalog.pg_database where"
            " datname = current_database()"
        ).fetchone()
        assert owner is not None
        db_conn.execute(sql.SQL("set local role {}").format(sql.Identifier(owner[0])))
        db_conn.execute("create table ops.v11_future_table (id uuid primary key)")
        for role in (*CLIENT_ROLES, "suv_backend"):
            assert db_conn.execute(
                "select has_table_privilege(%s, 'ops.v11_future_table', 'SELECT')", (role,)
            ).fetchone() == (False,)


def test_append_only_new_tables_reject_update_and_delete_for_everyone(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld
) -> None:
    rows = _populate(db_conn, seed, iw)
    for table in APPEND_ONLY_V11:
        row_id = rows[table]
        for statement in (
            sql.SQL("update {} set created_at = created_at where id = {}"),
            sql.SQL("delete from {} where id = {}"),
        ):
            query = statement.format(table_ident(table), sql.Literal(row_id))
            with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, iw.workspace_id):
                db_conn.execute(query)
            with expect_sqlstate("SV001"):
                db_conn.execute(query)


# ---------------------------------------------------------------------------------------------
# Workspace isolation for every new table
# ---------------------------------------------------------------------------------------------


def _populate(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> dict[str, uuid.UUID]:
    """One row in every new table of workspace ``iw`` (legitimate flow where it matters)."""
    rows: dict[str, uuid.UUID] = {
        "app.seller_entities": iw.seller_entity_id,
        "app.seller_contacts": iw.contact_id,
        "app.seller_inquiry_authorizations": iw.authorization_id,
        "app.seller_inquiry_controls": iw.controls_id,
        "ops.email_sender_bindings": iw.sender_binding_id,
    }
    rows["app.seller_entity_aliases"] = seed.insert_id(
        "app.seller_entity_aliases",
        workspace_id=iw.workspace_id,
        seller_entity_id=iw.seller_entity_id,
        source_id=iw.vehicle.source_id,
        alias_kind="marketplace_seller_id",
        reference="SYN-DEALER-42",
        alias_key_hash=sha(f"marketplace_seller_id:{iw.vehicle.source_key}:SYN-DEALER-42"),
        evidence_kind="listing_seller_block",
        observed_at=T0,
    )
    inquiry, attempt = sent_inquiry(db_conn, iw)
    rows["app.seller_inquiries"] = inquiry
    rows["ops.email_delivery_attempts"] = attempt
    ledger = db_conn.execute(
        "select id from ops.inquiry_quota_ledger where inquiry_id = %s", (inquiry,)
    ).fetchone()
    assert ledger is not None
    rows["ops.inquiry_quota_ledger"] = ledger[0]
    box = mailbox(seed, iw)
    rows["ops.mail_worker_bindings"] = box
    publish_binding(db_conn, iw, box, inquiry)
    sync = db_conn.execute(
        "select id from ops.mail_binding_sync where inquiry_id = %s", (inquiry,)
    ).fetchone()
    assert sync is not None
    rows["ops.mail_binding_sync"] = sync[0]
    credential = db_conn.execute(
        "select credential_id from ops.mail_worker_bindings where id = %s", (box,)
    ).fetchone()
    assert credential is not None
    with backend(db_conn, iw.workspace_id):
        reply = insert_reply(db_conn, reply_values(iw, inquiry, box))
        rows["app.seller_replies"] = reply
        rows["app.seller_reply_locators"] = insert_row(
            db_conn,
            "app.seller_reply_locators",
            {
                "workspace_id": iw.workspace_id,
                "reply_id": reply,
                "mailbox_binding_id": box,
                "outlook_entry_id": "ENTRYSYN01",
                "seen_at": T0,
            },
        )
        rows["ops.mail_ingest_dedup"] = insert_row(
            db_conn,
            "ops.mail_ingest_dedup",
            {
                "workspace_id": iw.workspace_id,
                "mailbox_binding_id": box,
                "credential_id": credential[0],
                "dedup_kind": "content_hash",
                "dedup_key": f"{box}:content_hash:{sha('x')}",
                "idempotency_key": "idem-populate-01",
                "fingerprint": sha("x"),
                "fingerprint_version": "reply-source-fingerprint/1",
                "inquiry_id": inquiry,
                "reply_id": reply,
                "ingest_result": "stored",
            },
        )
        rows["ops.mail_worker_checkpoints"] = insert_row(
            db_conn,
            "ops.mail_worker_checkpoints",
            {
                "workspace_id": iw.workspace_id,
                "mailbox_binding_id": box,
                "store_id_hash": sha("store"),
                "folder_id_hash": sha("folder"),
            },
        )
        rows["app.availability_events"] = insert_row(
            db_conn,
            "app.availability_events",
            {
                "workspace_id": iw.workspace_id,
                "source_id": iw.vehicle.source_id,
                "listing_id": iw.listing_id,
                "old_availability": "available",
                "new_availability": "available",
                "evidence_kind": "seller_reported_available",
                "reason": "seller_reported_available",
                "reply_id": reply,
                "effective_at": T0,
                "observed_at": T0,
                "confidence": "medium",
            },
        )
    rows["ops.email_suppressions"] = suppression(
        seed, iw.workspace_id, "seller", f"seller_entity:{uuid.uuid4()}"
    )
    assert set(rows) == set(V11_TABLES)
    return rows


def test_every_new_table_is_isolated_by_workspace(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    rows = _populate(db_conn, seed, iw)
    for table, row_id in rows.items():
        count = sql.SQL("select count(*) from {} where id = {}").format(
            table_ident(table), sql.Literal(row_id)
        )
        with backend(db_conn, iw.workspace_id):
            assert db_conn.execute(count).fetchone() == (1,), table
        with backend(db_conn, iw_b.workspace_id):
            assert db_conn.execute(count).fetchone() == (0,), table
        with backend(db_conn):
            assert db_conn.execute(count).fetchone() == (0,), table
        if table not in APPEND_ONLY_V11:
            touch = sql.SQL("update {} set created_at = created_at where id = {}").format(
                table_ident(table), sql.Literal(row_id)
            )
            # Not even an UPDATE privilege on created_at: refused before RLS is consulted.
            with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, iw_b.workspace_id):
                db_conn.execute(touch)


def test_backend_cannot_write_into_another_workspace(
    db_conn: psycopg.Connection, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    for statement, params in (
        (
            "insert into app.seller_entities (workspace_id, seller_type) values (%s, 'dealer')",
            (iw.workspace_id,),
        ),
        (
            "insert into ops.email_suppressions (workspace_id, scope, scope_key, reason, created_by_kind)"
            " values (%s, 'workspace', '*', 'kill_switch', 'system')",
            (iw.workspace_id,),
        ),
        (
            "insert into app.seller_inquiry_controls (workspace_id) values (%s)",
            (iw.workspace_id,),
        ),
    ):
        with pytest.raises(errors.InsufficientPrivilege), backend(db_conn, iw_b.workspace_id):
            db_conn.execute(statement, params)
    # Pausing another workspace's inquiries is invisible (0 rows), never an error leak.
    with backend(db_conn, iw_b.workspace_id):
        changed = db_conn.execute(
            "update app.seller_inquiry_controls set kill_switch = true, kill_switch_reason = 'cross pause',"
            " kill_switch_set_at = now(), kill_switch_set_by = %s, version = version + 1"
            " where workspace_id = %s",
            (uuid.uuid4(), iw.workspace_id),
        ).rowcount
    assert changed == 0


def test_composite_keys_reject_cross_workspace_links(
    db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld, iw_b: InquiryWorld
) -> None:
    with expect_sqlstate("23503"):
        seed.insert(
            "app.seller_contacts",
            workspace_id=iw.workspace_id,
            source_id=iw_b.vehicle.source_id,
            listing_id=iw_b.listing_id,
            listing_revision_number=1,
            seller_entity_id=iw.seller_entity_id,
            address="x@synthetic-dealer.example",
            contact_kind="ad_email",
            evidence_kind="email_on_advertisement",
            listing_reference="SYN-X",
            listing_url="https://synthetic-dealer.example/vehicles/SYN-X",
            extraction_location="listing_contact_block",
            rules_version="seller_contacts@1.0.0",
            observed_at=T0,
        )
    with expect_sqlstate("23503"):
        suppression(
            seed, iw.workspace_id, "seller", f"seller_entity:{iw.seller_entity_id}", inquiry_id=uuid.uuid4()
        )


# ---------------------------------------------------------------------------------------------
# API credential scopes mirror domain.actor.ROLE_SCOPES
# ---------------------------------------------------------------------------------------------


def _credential_allowed(
    db_conn: psycopg.Connection, workspace_id: uuid.UUID, role: str, scopes: list[str]
) -> bool:
    try:
        with db_conn.transaction(force_rollback=True):
            db_conn.execute(
                "insert into ops.api_credentials (workspace_id, principal_id, principal_kind, role,"
                " credential_kind, token_hash, scopes, label, expires_at)"
                " values (%s, %s, 'mcp_client', %s, 'static_bearer', %s, %s, 'Synthetic scope probe', %s)",
                (
                    workspace_id,
                    uuid.uuid4(),
                    role,
                    sha(str(uuid.uuid4())),
                    scopes,
                    datetime.now(UTC) + timedelta(days=1),
                ),
            )
        return True
    except errors.CheckViolation:
        return False


@pytest.mark.parametrize("role", [r.value for r in Role])
@pytest.mark.parametrize("scope", [s.value for s in Scope])
def test_single_scope_acceptance_mirrors_the_domain(
    db_conn: psycopg.Connection, iw: InquiryWorld, role: str, scope: str
) -> None:
    expected = Scope(scope) in ROLE_SCOPES[Role(role)] and (scope != "mail:ingest" or role == "owner")
    assert _credential_allowed(db_conn, iw.workspace_id, role, [scope]) is expected


def test_full_role_scope_sets(db_conn: psycopg.Connection, iw: InquiryWorld) -> None:
    for role, scopes in ROLE_SCOPES.items():
        usable = sorted(s.value for s in scopes if s != Scope.MAIL_INGEST)
        assert _credential_allowed(db_conn, iw.workspace_id, role.value, usable), role
    assert not _credential_allowed(
        db_conn, iw.workspace_id, "owner", sorted(s.value for s in ROLE_SCOPES[Role.OWNER])
    )
    assert not _credential_allowed(
        db_conn, iw.workspace_id, "reviewer", ["inquiries:read", "inquiries:pause"]
    )
    assert not _credential_allowed(db_conn, iw.workspace_id, "viewer", ["inquiries:read"])
    assert _credential_allowed(db_conn, iw.workspace_id, "owner", ["inquiries:pause"])
    assert not _credential_allowed(db_conn, iw.workspace_id, "owner", ["mail:ingest", "inquiries:pause"])
    assert not _credential_allowed(db_conn, iw.workspace_id, "owner", ["inquiries:write"])


def test_scope_constraints_are_validated(db_conn: psycopg.Connection) -> None:
    rows = db_conn.execute(
        "select conname, convalidated from pg_constraint where conrelid = 'ops.api_credentials'::regclass"
        " and conname in ('api_credentials_scopes_ck', 'api_credentials_role_scopes_ck',"
        " 'api_credentials_mail_ingest_ck') order by conname"
    ).fetchall()
    assert rows == [
        ("api_credentials_mail_ingest_ck", True),
        ("api_credentials_role_scopes_ck", True),
        ("api_credentials_scopes_ck", True),
    ]


# ---------------------------------------------------------------------------------------------
# SQL helpers agree with the Python domain
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_message_body_hash_matches_the_domain_for_every_template(
    db_conn: psycopg.Connection, template_id: str
) -> None:
    template = TEMPLATES[template_id]
    row = db_conn.execute(
        "select app.message_body_hash(%s, %s)", (template.subject, template.body)
    ).fetchone()
    assert row == (message_body_hash(template.subject, template.body),)


@pytest.mark.parametrize("language", ["de", "it", "fr", "en"])
def test_message_body_hash_matches_rendered_messages(db_conn: psycopg.Connection, language: str) -> None:
    message, preview = rendered(language, "SYN-A-1", "https://synthetic-dealer.example/v/1?id=7")
    for subject, body, expected in (
        (message.subject, message.body, message.body_hash),
        (preview.subject, preview.body, preview.body_hash),
        (
            message.subject,
            message.body.replace("\n", "\r\n"),
            message.body_hash,
        ),  # CRLF normalised like the domain
    ):
        assert db_conn.execute("select app.message_body_hash(%s, %s)", (subject, body)).fetchone() == (
            expected,
        )


def test_validation_helpers(db_conn: psycopg.Connection) -> None:
    cases: list[tuple[str, Any, bool]] = [
        ("app.email_address_ok(%s)", "Max.Mustermann+x@example.de", True),
        ("app.email_address_ok(%s)", "max@Example.de", False),
        ("app.email_address_ok(%s)", "a..b@example.de", False),
        ("app.email_address_ok(%s)", ".a@example.de", False),
        ("app.email_address_ok(%s)", "a@example", False),
        ("app.email_address_ok(%s)", "a b@example.de", False),
        ("app.email_address_ok(%s)", "a@xn--mnchen-3ya.de", True),
        ("app.rfc_message_id_ok(%s)", "<A.b-1@mail.example.de>", True),
        ("app.rfc_message_id_ok(%s)", "<a@Mail.example.de>", False),
        ("app.rfc_message_id_ok(%s)", "<a@b@c>", False),
        ("app.rfc_message_id_ok(%s)", "<a b@c.de>", False),
        ("app.rfc_message_id_ok(%s)", "a@c.de", False),
        ("app.sender_display_name_ok(%s)", "Vasko K.", True),
        ("app.sender_display_name_ok(%s)", "Name\r\nBcc: x", False),
        ("app.sender_display_name_ok(%s)", " Name", False),
        ("app.sender_display_name_ok(%s)", "x" * 65, False),
        ("app.sender_display_name_ok(%s)", "Name <a@b.de>", False),
        ("app.opaque_ref_ok(%s, 10)", "abc", True),
        ("app.opaque_ref_ok(%s, 10)", "a b", False),
        ("app.opaque_ref_ok(%s, 2)", "abc", False),
    ]
    for expression, value, expected in cases:
        row = db_conn.execute(f"select {expression}", (value,)).fetchone()
        assert row == (expected,), (expression, value)


def test_audit_helper_targets(db_conn: psycopg.Connection, seed: Seed, iw: InquiryWorld) -> None:
    """Sanity check of the synthetic audit builder used by the removal/requalification tests."""
    audit = audit_event(
        seed, iw.workspace_id, "email_suppression", iw.workspace_id, "email_suppression.removed"
    )
    assert db_conn.execute("select target_type from ops.audit_events where id = %s", (audit,)).fetchone() == (
        "email_suppression",
    )
