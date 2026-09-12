-- Lab-open plan (2026-09-12) — L1 of the private-note "三層防護":
-- Postgres Row Level Security on notes / note_chunks / figures.
--
-- Apply ONCE, as the table-owning/superuser role (e.g. `postgres`), AFTER
-- postgres_schema.sql — never by the application at connection-open time
-- (postgres_store.py's _apply_schema() explicitly skips this file when
-- SB_MULTIUSER=1; see store/migrate_multiuser.py, the intended way to run
-- this file). It is NOT run by _apply_schema for SB_MULTIUSER-unset
-- deployments either — :9100/:9106 must never get RLS enabled on their
-- tables even by accident, since a non-superuser app role has not been
-- provisioned for them.
--
-- Why RLS instead of a WHERE clause added to every query: postgres_store.py
-- has 50+ query sites touching notes/note_chunks/figures; a query added
-- after this migration that forgets the WHERE clause is a silent leak, and
-- the next one will forget it too. RLS makes the database itself refuse to
-- return or accept a row the connection's `sb.actor_id` cannot see, for
-- every query regardless of which Python path wrote it — see visibility.py's
-- module docstring.
--
-- Policy: owner_id IS NULL (shared) OR owner_id = sb.actor_id (own private)
-- OR sb.actor_is_admin (admin audit read — see identity.Identity.is_admin
-- and postgres_store.py's _conn(), which sets both GUCs from the resolved
-- Identity every checkout). Both GUCs are namespaced (contain a '.') so
-- Postgres accepts SET/set_config on them without any extension declaring
-- them first.
--
-- sb_app is intentionally NOT the table owner and NOT given BYPASSRLS —
-- Postgres superusers and BYPASSRLS roles ignore RLS unconditionally, FORCE
-- ROW LEVEL SECURITY included. If lcdda's SB_PG_DSN ever points back at a
-- superuser role, every policy below silently does nothing — this is *the*
-- failure mode the plan calls out. Verify with the bypass test (Phase 4 #5):
-- connect as sb_app, SET sb.actor_id to one member, and confirm
-- `SELECT count(*) FROM notes` is smaller than the same query as `postgres`.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sb_app') THEN
        -- No PASSWORD here on purpose — a schema file is not where a secret
        -- belongs. A deployment sets one out of band (ALTER ROLE sb_app
        -- PASSWORD '<from Keychain>') before pointing SB_PG_DSN at it.
        CREATE ROLE sb_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOBYPASSRLS;
    END IF;
END $$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO sb_app', current_database());
END $$;

-- ---------------------------------------------------------------------------
-- note_chunks.owner_id / figures.owner_id already exist by this point —
-- postgres_schema.sql (applied just before this file, see
-- migrate_multiuser.py) adds both columns unconditionally, because
-- postgres_store.py writes them on every note sync regardless of
-- SB_MULTIUSER. What's left here is a one-time backfill for any row written
-- before this migration ever ran (owner_id always derives purely from path,
-- so this recomputes exactly what postgres_store.py would have written had
-- the column existed already).
-- ---------------------------------------------------------------------------
UPDATE note_chunks SET owner_id = notes.owner_id
FROM notes WHERE notes.path = note_chunks.note_path AND note_chunks.owner_id IS DISTINCT FROM notes.owner_id;
UPDATE figures SET owner_id = notes.owner_id
FROM notes WHERE notes.path = figures.note_path AND figures.owner_id IS DISTINCT FROM notes.owner_id;

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

ALTER TABLE notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE notes FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sb_owner_visibility ON notes;
CREATE POLICY sb_owner_visibility ON notes
    USING (
        owner_id IS NULL
        OR owner_id = NULLIF(current_setting('sb.actor_id', true), '')::uuid
        OR current_setting('sb.actor_is_admin', true) = 'on'
    )
    WITH CHECK (
        owner_id IS NULL
        OR owner_id = NULLIF(current_setting('sb.actor_id', true), '')::uuid
        OR current_setting('sb.actor_is_admin', true) = 'on'
    );

ALTER TABLE note_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE note_chunks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sb_owner_visibility ON note_chunks;
CREATE POLICY sb_owner_visibility ON note_chunks
    USING (
        owner_id IS NULL
        OR owner_id = NULLIF(current_setting('sb.actor_id', true), '')::uuid
        OR current_setting('sb.actor_is_admin', true) = 'on'
    )
    WITH CHECK (
        owner_id IS NULL
        OR owner_id = NULLIF(current_setting('sb.actor_id', true), '')::uuid
        OR current_setting('sb.actor_is_admin', true) = 'on'
    );

ALTER TABLE figures ENABLE ROW LEVEL SECURITY;
ALTER TABLE figures FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS sb_owner_visibility ON figures;
CREATE POLICY sb_owner_visibility ON figures
    USING (
        owner_id IS NULL
        OR owner_id = NULLIF(current_setting('sb.actor_id', true), '')::uuid
        OR current_setting('sb.actor_is_admin', true) = 'on'
    )
    WITH CHECK (
        owner_id IS NULL
        OR owner_id = NULLIF(current_setting('sb.actor_id', true), '')::uuid
        OR current_setting('sb.actor_is_admin', true) = 'on'
    );

-- ---------------------------------------------------------------------------
-- sb_app grants — CRUD + sequences on the tables the live server touches.
-- No CREATE/ALTER/schema-DDL rights, no BYPASSRLS: sb_app cannot run
-- postgres_schema.sql or this file itself (see postgres_store.py's
-- _verify_multiuser_schema, which is why _apply_schema does not try).
-- ---------------------------------------------------------------------------

GRANT USAGE ON SCHEMA public TO sb_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON notes, note_chunks, figures, api_keys, audit_log TO sb_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sb_app;
