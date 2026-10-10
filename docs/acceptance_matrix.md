# Acceptance matrix (spec 31, spec 33 M8 exit, spec 37.10 delta tests)

Every row of the spec 31 test matrix and every spec 37.10 delta test, mapped to concrete tests
and commands, with two judgements:

- **Result** (spec 33 M8 wording): **passed**, **failed**, **blocked** or **not run**. "Passed"
  means the automated tests pass on the working tree of 2026-10-10 (base `e5d47f4` plus the
  uncommitted waves D1-D3 and the final verification pass), on PostgreSQL 16 AND 17.11 wherever a
  database is involved (measured counts, and the D3 flaky test fixed since:
  [qa_evidence.md](qa_evidence.md)). It is not an exact-build result: no release report on a
  commit exists yet.
- **Class**: *offline-verified* (fully covered by the automated tests here), *activation-pending*
  (the offline part is covered; the remaining evidence needs a live service or the owner's
  accounts, see [ACTIVATION_GATES.md](../ACTIVATION_GATES.md)), or *gap* (missing test or tool).

Fixture and fake-based tests establish deterministic behaviour; they do not prove live source
access, external delivery or dot integration (spec 31).

How to run a row: `uv run pytest -q <paths>` (PostgreSQL 16; the database tests use
`TEST_DATABASE_ADMIN_URL`, default `postgresql://suv:suv@127.0.0.1:5432/postgres`); for
PostgreSQL 17 prefix `TEST_DATABASE_ADMIN_URL=postgresql://suv:suv@127.0.0.1:5433/postgres
TEST_DATABASE_MIGRATOR_ROLE=suv_migrator`. Desktop: `uv run pytest -q desktop/outlook-bridge/tests`.
Dashboard: `cd dashboard && npm test` (Vitest) and `npx playwright test` (browser E2E, with
`TEST_DATABASE_ADMIN_URL` set). Every command with its counts: [qa_evidence.md](qa_evidence.md).

## Spec 31 test matrix

| Area | Result | Class | Evidence (tests) | Not covered |
|---|---|---|---|---|
| Locale parsing | **passed** | offline-verified | `tests/unit/test_parsing.py`, `tests/property/test_parsing_properties.py`, `tests/unit/test_switzerland_market.py`, `tests/unit/test_parsing_inspection.py` (DE/IT/CH separators, apostrophes, currencies, ambiguity) | - |
| Mileage | **passed** | offline-verified | `tests/unit/test_filters.py` (199,999 passes, 200,000 fails), `tests/property/test_filters_properties.py`, `tests/unit/test_parsing.py` (miles, ranges, conflicts) | - |
| Prices | **passed** | offline-verified | `tests/unit/test_parsing.py` (instalment, net/export, negotiable), `tests/adapters/test_dealer_detail.py` (margin scheme, instalment, gross/net ambiguity), `tests/unit/test_parsing_inspection.py` | - |
| Dates | **passed** | offline-verified | `tests/unit/test_parsing.py` (source zones, DST gap, date-only precision, future/invalid) | - |
| Identity | **passed** | offline-verified | `tests/unit/test_identity.py` (tracking parameters, aliases), `tests/integration/repos_sources_listings/test_search_ingest.py` (hash collision quarantined), `tests/integration/repos_sources_listings/test_evidence_clusters.py` | - |
| Revisions | **passed** | offline-verified | `tests/integration/repos_sources_listings/test_search_ingest.py` (duplicate observation), `tests/integration/repos_sources_listings/test_detail_ingest.py` (unchanged content, semantic change, price reversion) | - |
| Eligibility | **passed** | offline-verified | `tests/unit/test_filters.py`, `tests/property/test_filters_properties.py` (primary 2,500-3,000, optional 4,000 profile disabled, unknown required facts) | - |
| Comparables | **passed** | activation-pending | `tests/unit/test_comparables.py` (wrong generation/engine/gearbox/drive, duplicates, sparse/stale), `tests/property/test_comparables_properties.py`, `tests/integration/pipeline/test_d2_mk_comparable_routing.py`, `tests/unit/test_d2_market_import.py` | no MK evidence in any real workspace: every real valuation is `insufficient_comparables` until the owner imports some (`suv-deals market import`) or an MK adapter is activated |
| Tax | **passed** (engine) / **blocked** (real use) | activation-pending | `tests/unit/test_tax_engine.py` (missing input, unapproved/expired rule, bracket/cycle/date boundaries, synthetic golden fixtures) | no approved, sourced rule set (gate `tax_rules`); no command stores/approves a rule set in the database |
| Economics | **passed** | offline-verified | `tests/unit/test_costs.py`, `tests/unit/test_valuation.py`, `tests/property/test_costs_properties.py` (Decimal rounding, unknown never zero, reserve, deposit/refund, no double count) | owner cost assumptions (gate `cost_assumptions`) |
| FX | **passed** | offline-verified | `tests/unit/test_fx.py` (direction, CHF conversion, stale rate, threshold edge), `tests/integration/pipeline/test_d2_fx_refresh.py`, `tests/cli/test_d2_fx_commands.py` | no live ECB fetch was made (network off) |
| Queue | **passed** | offline-verified | `tests/integration/persistence_core/test_jobs.py` (crash before/after commit, lost lease, retry, dead letter), `tests/integration/db/test_queue.py`, `tests/integration/pipeline/test_runner.py` | - |
| Scheduler | **passed** | offline-verified | `tests/integration/repos_sources_listings/test_schedules.py` (two concurrent schedulers, skipped slot, downtime catch-up without burst), `tests/integration/pipeline/test_scheduler.py` | - |
| Outbox | **passed** | activation-pending | `tests/integration/persistence_core/test_outbox.py` (timeout after acceptance, crash after send), `tests/integration/pipeline/test_dispatcher.py`, `tests/integration/v11_runtime/test_signal_delivery.py` (stale suppression, dedup, reconcile) | provider contracts against fakes only; no real destination (gate `production_notifications`) |
| RLS | **passed** | offline-verified | `tests/integration/db/test_rls_and_grants.py` (anonymous, member, non-member, cross-workspace FKs), `tests/integration/db/test_backend_membership.py`, `tests/integration/repos_sources_listings/test_rls_isolation.py`, `tests/integration/read_queries/test_isolation.py` | the hosted project was checked only through the migration post-checks (docs/schema.md section 9) |
| API/MCP auth | **passed** | activation-pending | `tests/api/test_auth_tokens.py` (expired, issuer, audience), `tests/api/test_http_auth.py`, `tests/mcp/test_auth.py` (scope denial, revocation), `tests/mcp/test_preauth.py` | no real Supabase issuer or dot client (gate `mcp_authentication`) |
| MCP tools | **passed** (schemas, positive/negative, frozen pagination, idempotency) / **blocked** (real client transcript) | activation-pending | `tests/mcp/test_tools.py`, `tests/mcp/test_v11_tools.py`, `tests/mcp/test_c2_tools.py`, `tests/contracts/test_mcp_tool_schemas.py`, `tests/contracts/test_v11_tool_schemas.py` | an Inspector or real-client transcript (gate `mcp_authentication`) |
| SSRF | **passed** | activation-pending | `tests/adversarial/test_crawl_ssrf.py`, `tests/adversarial/test_callback_ssrf.py`, `tests/adversarial/test_netguard.py`, `tests/unit/test_safe_http.py`, `tests/unit/test_url_policy.py` | the host firewall rules (docs/runbook.md 3.2) are documented, not executed (no Docker here) |
| Seller email | **passed** (contract, integration, E2E with fakes) / **blocked** (live) | activation-pending | `tests/integration/v11_db`, `tests/integration/v11_inquiries`, `tests/integration/v11_runtime`, `tests/integration/v11_replies`, `tests/integration/mail_worker_e2e`, `tests/contracts/test_mail_worker_contract.py`, `desktop/outlook-bridge/tests`, `tests/unit/test_mime_builder.py`, `tests/unit/test_seller_templates.py` (section below) | no real mailbox, Outlook or provider; activation canary rows 4-6 (gates `seller_email_sender`, `seller_inquiry`) |
| Event ingress | **passed** | activation-pending | `tests/adversarial/test_event_ingress.py` (signature over the raw body, stale replay, duplicate, wrong channel/app, own-event loop), `tests/unit/test_webhook_signing.py`, `tests/unit/test_slack.py` | no real Slack app (gate `seller_reply_slack_route`) |
| Injection | **passed** | offline-verified | `tests/adversarial/test_email_injection.py`, `tests/adversarial/test_event_ingress.py`, `tests/unit/test_replies.py`, `dashboard/e2e/browse.spec.ts` (XSS payloads inert), `dashboard/src/test/v11Security.test.tsx` | - |
| Dashboard | **passed** | activation-pending | `dashboard/e2e/review.spec.ts` (double submit, expired claim, a new revision before submit), `dashboard/e2e/browse.spec.ts`, `dashboard/e2e/inquiries.spec.ts`, `dashboard/e2e/auth.spec.ts`, `dashboard/e2e/security.spec.ts`, `dashboard/src/test` | browser E2E runs against mock Supabase Auth and a local backend, never a deployed dashboard; the open process gate is not exercised in the browser |
| Source adapter | **passed** (saved fixtures) / **blocked** (live smoke) | activation-pending | `tests/adapters` (search/detail variants, zero results, removed, login wall, CAPTCHA, malformed markup; `tests/adapters/test_fixture_manifests.py`), `tests/integration/pipeline/test_e2e_fixture_pipeline.py` | 12 of 14 sources have no adapter; no live smoke (gate `source_access`) |
| Backup | **passed** (local round trip) / **not run** (restore report of the real project) | activation-pending | `tests/cli/test_ops_files.py` (backup of a synthetic database and isolated restore check with counts and hashes; no password in argv) | no restore drill of the hosted project; storage objects not covered by a database backup; the manifest covers v1.0 tables only (OPS-13) |
| Deployment | **not run** | gap | `tests/cli/test_d2_release_verifier.py`, `tests/cli/test_ops_files.py`, `tests/integration/db/test_scripts.py` (forward-only migrations, rollback policy) | no release report on an exact commit, no clean install on a host, no rollback drill, no image digest (gate `hosting`) |
| Full pipeline | **blocked** | activation-pending | `tests/integration/pipeline/test_e2e_fixture_pipeline.py` (synthetic listing to DB to review: passed), `tests/integration/v11_runtime/test_e2e_runtime.py` | a live new/changed listing to an approved destination with dot's evidence (spec 31 "exact-build end-to-end acceptance") |

## Spec 37.10 required delta tests

| Required behaviour | Result | Class | Evidence (test file, test name) |
|---|---|---|---|
| A candidate lacking CoC can still qualify for the bounded inquiry | **passed** | offline-verified | `tests/unit/test_inquiries.py` (`test_candidate_lacking_coc_and_documents_is_inquiry_ready`, `test_documents_status_never_blocks_the_inquiry`), `tests/integration/v11_inquiries/test_qualification_and_reservation.py` (`test_candidate_lacking_coc_is_still_reserved`) |
| Unknown seller address or language cannot qualify | **passed** | offline-verified | `tests/unit/test_inquiries.py` (`test_unknown_or_unresolved_language_cannot_qualify`), `tests/integration/v11_inquiries/test_qualification_and_reservation.py` (`test_unknown_address_cannot_be_reserved`) |
| No human-approval wait is inserted | **passed** | offline-verified | `tests/integration/v11_db/test_inquiry_guards.py` (`test_no_human_approval_is_needed_anywhere`), `tests/integration/v11_inquiries/test_qualification_and_reservation.py` (`test_there_is_no_approval_wait_state`), `tests/integration/v11_runtime/test_operator_and_policy.py` (`test_no_handler_code_path_produces_an_approval_wait`), `tests/unit/test_seller_email_providers.py` (`test_send_never_waits_for_approval_when_not_required`) |
| DE/IT/FR/verified-EN templates ask identical questions without commitments | **passed** | offline-verified | `tests/unit/test_seller_templates.py` (`test_all_languages_ask_identical_questions_without_commitments`, `test_scope_validator_rejects_commitments_and_extra_data`) |
| Swiss language comes from evidence | **passed** | offline-verified | `tests/unit/test_language.py` (`test_swiss_language_comes_from_text_evidence`, `test_bilingual_swiss_style_ad_is_mixed_even_outside_the_margin`) |
| Three cross-site ads/aliases produce one send | **passed** | offline-verified | `tests/unit/test_inquiries.py` (`test_three_cross_site_ads_and_aliases_produce_one_inquiry_identity`), `tests/integration/v11_inquiries/test_identity_and_merges.py` (`test_three_cross_site_aliases_are_one_seller_and_one_inquiry`), `tests/integration/v11_db/test_dispatch_rechecks_and_merges.py` |
| Concurrent workers and identity merges cannot double-send | **passed** | offline-verified | `tests/integration/v11_inquiries/test_concurrency.py` (`test_concurrent_reservations_through_two_aliases_yield_one`), `tests/integration/v11_inquiries/test_identity_and_merges.py` (`test_identity_merge_while_queued_cannot_double_send`), `tests/integration/v11_db/test_one_inquiry_and_quota.py` (`test_concurrent_dispatch_of_one_inquiry_sends_once`), `tests/integration/v11_inquiries/test_d2_single_claimant.py` |
| An uncertain timeout or a crash after send but before the receipt commit never triggers a blind resend; an empty Sent Items result cannot release the reservation | **passed** | offline-verified | `tests/integration/v11_inquiries/test_outcomes_and_reconciliation.py` (`test_crash_after_hand_over_reaped_to_uncertain_and_job_blocked`), `tests/integration/v11_db/test_inquiry_state_machine.py` (`test_empty_sent_items_search_cannot_release_the_reservation`), `tests/unit/test_seller_email_providers.py` (`test_gmail_reconcile_empty_search_is_not_proof`) |
| Changed price or availability cancels stale queued messages | **passed** | offline-verified | `tests/unit/test_inquiries.py` (`test_changed_price_or_availability_cancels_stale_queued_message`), `tests/integration/v11_inquiries/test_outcomes_and_reconciliation.py` (`test_price_change_cancels_the_stale_queued_inquiry`, `test_availability_change_cancels_pending_inquiries`) |
| Bounce/opt-out and the kill switch suppress sending | **passed** | offline-verified | `tests/integration/v11_replies/test_reply_ingest.py` (`test_an_opt_out_suppresses_seller_and_address`), `tests/integration/v11_db/test_reply_links_and_reconciliation.py` (bounce), `tests/integration/v11_db/test_inquiry_guards.py` (`test_kill_switch_stops_untransmitted_work`, `test_kill_switch_blocks_dispatch_of_queued_work`), `tests/integration/v11_runtime/test_plan_and_send.py` (`test_kill_switch_between_reserve_and_send_holds_everything`), `tests/api/test_mail_worker_routes.py` (`test_claim_refusals_kill_switch_and_not_now`) |
| Replies map by ids/headers, not by subject alone | **passed** | offline-verified | `tests/integration/v11_db/test_reply_links_and_reconciliation.py` (`test_subject_only_match_is_never_an_automatic_reply`), `desktop/outlook-bridge/tests/test_reconciliation_flows.py` (`test_subject_match_alone_never_correlates`), `tests/unit/test_replies.py` |
| Unrelated personal mail never leaves the local mailbox | **passed** | offline-verified | `desktop/outlook-bridge/tests/test_reconciliation_flows.py` (`test_unrelated_personal_mail_never_leaves_the_machine`), `desktop/outlook-bridge/tests/test_health_matching.py` (`test_own_messages_and_unrelated_mail_cannot_be_prepared_for_upload`), `tests/unit/test_seller_email_providers.py` (`test_gmail_message_from_the_owner_never_leaves_the_mailbox`) |
| NewMailEx startup gaps are recovered | **passed** | offline-verified | `desktop/outlook-bridge/tests/test_reconciliation_flows.py` (`test_startup_gap_without_new_mail_events_is_recovered_by_reconciliation`, `test_new_mail_event_overflow_falls_back_to_reconciliation`) |
| Moved messages are deduplicated | **passed** | offline-verified | `desktop/outlook-bridge/tests/test_reconciliation_flows.py` (`test_moved_message_is_deduplicated_and_its_locator_recorded`, `test_moved_message_without_internet_message_id_is_deduplicated`) |
| Sleep/offline outages show gaps and recover the backlog | **passed** | offline-verified | `desktop/outlook-bridge/tests/test_worker_flows.py` (`test_sleep_gap_is_recorded_and_mail_from_the_gap_is_recovered`, `test_offline_restart_records_the_gap_and_recovers_the_durable_backlog`), `tests/integration/mail_worker_e2e/test_desktop_worker_e2e.py` (`test_backend_outage_keeps_the_local_queue_and_replays_it`) |
| Slack receipts are separate from dot processing | **passed** (code) / **blocked** (dot evidence) | activation-pending | `tests/integration/v11_runtime/test_signal_delivery.py` (a Slack `ok` is recorded as a provider receipt only), `tests/integration/persistence_core/test_outbox.py` (`test_successful_delivery_records_receipt_and_separate_timestamps`) |
| MIME, attachment and header injection is rejected | **passed** | offline-verified | `tests/adversarial/test_email_injection.py`, `tests/unit/test_mime_builder.py` (`test_spec_has_no_cc_bcc_attachment_or_threading_fields`, `test_scope_recheck_refuses_free_text_and_swapped_renderings`, `test_reply_to_can_never_be_the_seller`) |
| No auto-follow-up, purchase, price acceptance or reservation occurs | **passed** | offline-verified | `tests/unit/test_inquiries.py` (`test_no_follow_up_is_possible_after_acceptance`), `tests/unit/test_replies.py` (`test_quote_can_never_be_marked_accepted`, `test_price_quote_invalidates_valuation_but_is_not_accepted`), `tests/unit/test_seller_templates.py`, `tests/mcp/test_v11_tools.py` (no send, reply, resume or approve tool) |

## Spec 37.10 activation evidence

| Evidence for the actual account and client | Result | Class | Where it is defined |
|---|---|---|---|
| Sender verification | **blocked** | activation-pending | docs/seller_email_activation.md section 8 row 1; ACTIVATION_GATES.md section 9 |
| The configured runtime (classic Outlook, worker revision = backend revision) | **blocked** | activation-pending | row 2 |
| A safe provider test message to an owner-controlled address (activation canary) | **blocked** | activation-pending | rows 4-5; `outlook_local` transport tested with fakes (`tests/integration/mail_worker_e2e/test_d2_canary_e2e.py`, `desktop/outlook-bridge/tests/test_d2_canary.py`); `gmail_api` / `microsoft_graph`: no transport (gap) |
| Receipt reconciliation | **blocked** | activation-pending | row 5 |
| A correlated test reply through Outlook, backend, Slack, dot and MCP | **blocked** | activation-pending | row 6 covers Outlook -> backend only; the Slack -> dot -> MCP leg belongs to gate `seller_reply_slack_route` (no canary signal built: gap) |
| The synthetic canary never counts as a seller inquiry or one of the 15-day deals | **passed** | offline-verified | `tests/integration/v11_inquiries/test_canaries.py`, `tests/unit/test_d2_canary_domain.py`, `tests/unit/test_evaluation.py` |

## Exact-build evidence

`docs/qa/<commit>/` is empty: the D1-D3 work is not committed, so no `scripts/verify_release.sh
--with-e2e` report on an exact commit exists ([qa/README.md](qa/README.md)). After the commit, run
it and copy `var/releases/<sha>_<ts>.txt` and the dashboard, desktop and E2E outputs into
`docs/qa/<sha>/` (docs/runbook.md section 8).
