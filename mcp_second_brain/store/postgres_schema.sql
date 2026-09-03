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

-- ---------------------------------------------------------------------------
-- note_chunks — Phase B of the chunking/embedding plan (late-chunked,
-- paragraph-aligned, ~512 tokens, overlap=0; see chunking.py/late_chunking.py).
-- ---------------------------------------------------------------------------
-- Maintained by the same write paths as `notes` (index_file/sync_all/
-- sync_incremental) — see postgres_store.py's _sync_chunks_for_note(). Rows are
-- fully replaced (DELETE + re-INSERT) on any content_hash change, never updated
-- in place. ON DELETE CASCADE means a note's chunks disappear automatically
-- whenever sync_all's reconciliation step removes the parent notes row (vault
-- file deleted / archived-and-pruned) — no separate chunk-cleanup call needed.
CREATE TABLE IF NOT EXISTS note_chunks (
    note_path    TEXT NOT NULL REFERENCES notes(path) ON DELETE CASCADE,
    chunk_idx    INTEGER NOT NULL,
    chunk_text   TEXT NOT NULL,
    content_hash TEXT,                 -- notes.content_hash at chunk build time — see decision 1
    embedding    vector(1024),         -- must equal vault_db.EMBED_DIM; test_embedding_dim.py
                                        -- guards both this and notes.embedding together.
    PRIMARY KEY (note_path, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_note_chunks_path ON note_chunks(note_path);

-- Keyword search on chunk_text — the point of Phase B's B-1: without this,
-- hybrid_search's keyword path stays limited to notes.body_snippet's leading
-- 500 chars no matter how far chunking extends semantic search's reach.
CREATE INDEX IF NOT EXISTS idx_note_chunks_trgm ON note_chunks
    USING gin (chunk_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_note_chunks_tsv ON note_chunks
    USING gin (to_tsvector('english', chunk_text));

-- Same HNSW parameters as idx_notes_embedding, for the same reason.
CREATE INDEX IF NOT EXISTS idx_note_chunks_embedding ON note_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 128);
