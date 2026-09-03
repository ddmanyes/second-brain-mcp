-- Postgres schema for second-brain vault index
-- Requires: pgvector (vector type + HNSW), pg_trgm (trigram GIN for CJK/keyword FTS)
-- Apply once per database; safe to re-run (all statements are IF NOT EXISTS).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- notes — primary vault index (one row per .md file)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    path                TEXT PRIMARY KEY,
    title               TEXT,
    note_type           TEXT,
    status              TEXT,
    tags                TEXT,
    note_date           DATE,
    content_hash        TEXT,
    access_count        INTEGER NOT NULL DEFAULT 0,
    last_accessed       TIMESTAMP,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    body_snippet        TEXT,
    snapshot_path       TEXT,
    snapshot_tier       TEXT,
    snapshot_token_est  INTEGER,
    semantic_keywords   TEXT,
    neighbor_keywords   TEXT,
    cluster_topic       TEXT,
    violations          TEXT,
    rules_extracted_at  TIMESTAMP,
    embedding           vector(1024)     -- bge-m3-Q8_0 (1024d). Must equal vault_db.EMBED_DIM;
                                         -- tests/test_embedding_dim.py pins the two together.
);

-- ---------------------------------------------------------------------------
-- figures — extracted figures from PDFs / notes
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS figures (
    id          BIGSERIAL PRIMARY KEY,
    note_path   TEXT NOT NULL,
    fig_index   INTEGER,
    image_url   TEXT,
    local_path  TEXT,
    ocr_text    TEXT,
    description TEXT,
    token_est   INTEGER,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- PDF pipeline Phase 2.5c: figure caption (detected during page-render extraction)
ALTER TABLE figures ADD COLUMN IF NOT EXISTS caption TEXT;

-- ---------------------------------------------------------------------------
-- Scalar indexes
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_last_accessed ON notes(last_accessed DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_note_date     ON notes(note_date DESC NULLS LAST);
CREATE INDEX IF NOT EXISTS idx_note_type     ON notes(note_type);
CREATE INDEX IF NOT EXISTS idx_status        ON notes(status);
CREATE INDEX IF NOT EXISTS idx_figures_note  ON figures(note_path);

-- ---------------------------------------------------------------------------
-- Keyword / FTS indexes
-- ---------------------------------------------------------------------------

-- Trigram GIN — language-neutral, works for CJK and English substrings.
-- Covers title + body_snippet + tags + semantic_keywords.
CREATE INDEX IF NOT EXISTS idx_notes_trgm ON notes USING gin(
    (
        COALESCE(title, '') || ' ' ||
        COALESCE(body_snippet, '') || ' ' ||
        COALESCE(tags, '') || ' ' ||
        COALESCE(semantic_keywords, '') || ' ' ||
        COALESCE(neighbor_keywords, '') || ' ' ||
        COALESCE(cluster_topic, '')
    ) gin_trgm_ops
);

-- tsvector GIN — English BM25-ish ranking via ts_rank.
-- Secondary to trgm; used for multi-word English phrases.
CREATE INDEX IF NOT EXISTS idx_notes_tsv ON notes USING gin(
    to_tsvector('english',
        COALESCE(title, '') || ' ' || COALESCE(body_snippet, '')
    )
);

-- ---------------------------------------------------------------------------
-- api_keys — per-key identity + role for RBAC (MULTIUSER_PLAN P1)
-- ---------------------------------------------------------------------------
-- key_hash: SHA-256(raw_key).hexdigest() — plaintext never stored.
-- role: 'reader' | 'writer' | 'admin'
-- revoked_at IS NOT NULL → key rejected.
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   TEXT        PRIMARY KEY,
    user_id    TEXT        NOT NULL,
    role       TEXT        NOT NULL DEFAULT 'reader',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);

-- ---------------------------------------------------------------------------
-- audit_log — immutable write-action record (MULTIUSER_PLAN P3)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id       BIGSERIAL    PRIMARY KEY,
    ts       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    user_id  TEXT         NOT NULL,           -- identity.user_id or 'unknown'
    tool     TEXT         NOT NULL,           -- write tool name
    target   TEXT         NOT NULL DEFAULT '' -- note_path / URL / title, or ''
);

CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_ts   ON audit_log(ts DESC);

-- ---------------------------------------------------------------------------
-- Vector similarity index (HNSW — sub-linear cosine ANN)
-- ---------------------------------------------------------------------------
-- Build after sync_all so the index is not rebuilt per-row during import.
-- ef_construction=128, m=16 are sensible defaults for a few-thousand-note vault.
CREATE INDEX IF NOT EXISTS idx_notes_embedding ON notes
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
