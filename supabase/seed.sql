-- =============================================================================
-- supabase/seed.sql  --  LOCAL DEVELOPMENT ONLY (synthetic)
-- =============================================================================
-- Applied by `supabase db reset` / `supabase start` on a LOCAL stack only
-- ([db.seed] in supabase/config.toml). Never apply to staging or production.
--
-- Creates exactly one clearly labelled synthetic workspace so a developer can
-- attach a local user membership by hand. It deliberately creates:
--   * no users and no memberships (create a local auth user, then insert a
--     membership yourself),
--   * no API credentials or secrets,
--   * no sources, listings, valuations or any real-looking market data.
-- Idempotent: running it twice leaves one row.
-- =============================================================================

insert into app.workspaces (id, name, display_timezone, active)
values ('00000000-0000-4000-8000-00000000d001', 'Local development (synthetic)', 'Europe/Skopje', true)
on conflict (id) do nothing;
