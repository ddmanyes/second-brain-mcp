# Changelog

All notable source-tree changes are recorded here.

## Unreleased

### Changed

- `embed_text_for()` now strips the References section before capping length
  (`snippets.strip_references` — cited-paper titles are not the note's own claims)
  and its `max_chars` moved from 900 to ~32,000, aligned with bge-m3's 8,192-token
  context. Fixed alongside it: `note_row.LARGE_FILE_READ_LIMIT` moved from 16 KB to
  40 KB — it sat well under the old 900-char cap, so raising max_chars alone would
  have been a silent no-op for every note over the 32 KB large-file threshold
  (most research notes; median is 81,074 chars). Phase B-0 of
  `10-projects/second-brain/phases/second-brain-分塊-embedding-與-late-chunking-實施計畫.md`.
  Removed a dead, unused duplicate of the old read-limit constants left over in
  `vault_db.py` from before the `note_row` extraction.

### Added

- Added the read-only `audit_article_records` MCP tool with bounded parameters,
  structured output, and an equivalent text fallback for older clients.
- Added article housekeeping checks for required frontmatter, broken wikilinks,
  exact duplicate candidates, overdue inbox records, and social-source freshness.
- Added actionable warnings for unreadable vaults, invalid source state, and invalid
  parameter bounds without performing automatic merge, archive, or deletion.
- Added regression tests pinning the `get_agent_instructions()` two-layer contract:
  no not-found placeholder, base-layer markers present, personal layer appended after
  the base layer, and no crash when the vault has no `AGENTS.md`.
- Added a packaging test that builds the wheel and asserts the shipped artifact
  actually contains `mcp_second_brain/AGENTS.md` and the console entry point, so a
  file dropping out of the distribution fails the build instead of degrading silently
  in production.
- Added two personal-layer `AGENTS.md` samples (general-purpose vault and
  literature/knowledge-graph vault) under `templates/`, documenting how the two vault
  profiles diverge.

### Changed

- Folded `search_snippets`, `query_graph`, and `litnet_answer` into the §B search SOP;
  they were listed in the tool table but absent from the documented retrieval sequence.
  Added a note that structured graph edges only exist in a vault that has a
  `.graph/statements.jsonl`, so an empty edge result is not evidence of silent literature.
- Replaced host-specific identifiers (Tailscale address, SSH account, home paths) in the
  documentation with placeholders.

### Fixed

- Fixed `get_agent_instructions()` returning only the not-found placeholder plus the
  personal layer for every installed (non-editable) deployment: the wheel build packaged
  only `mcp_second_brain`, so the repo-root `AGENTS.md` never shipped and both candidate
  paths missed. The base layer — tool reference, recall ladder, SOPs, safety rules — never
  reached remote agents, and the failure was silent because the personal layer was still
  appended after the placeholder. Fixed with a `force-include` that populates the packaged
  candidate path `server.py` already probed.
- Fixed note snapshot rendering from async MCP requests by running the synchronous
  Playwright renderer in a worker thread.
- Aligned article-audit Markdown exclusions with the indexer so `.obsidian`, `.claude`,
  and `templates` do not produce false-positive `index_gap` values.
- Replaced the stalled 30-minute launchd interval with calendar triggers at minute 0
  and 30 in the pg-sync deployment template.

### Added

- Added Phase B of the chunking/embedding plan: a `note_chunks` table (paragraph-aligned,
  late-chunked embeddings — `chunking.py`, `late_chunking.py`) alongside the existing
  single-vector `notes.embedding`, plus HNSW/trgm/tsvector indexes. `hybrid_search()`
  queries both layers and merges by path so a note without chunks yet (mid-backfill, or a
  transient late-chunking-server outage) never disappears from search. A dedicated
  `--pooling none` llama-server instance (`com.llama-server-late-chunking`, :8082) does the
  late-chunking encode — `--pooling` is a launch-time flag, not per-request, so it can't
  share the existing embedding server. New `sync_chunks(vault, limit=None)` backfill path
  and `sync_chunks_tool(limit=200)` MCP tool for draining a backlog in bounded batches
  (`sync_index()` itself now only processes a small bounded slice per call, for the same
  reason). See `10-projects/second-brain/phases/second-brain-分塊-embedding-與-late-chunking-實施計畫.md`.
- Added `reranker.py`: reranks `hybrid_search()`'s fused candidates via a dedicated
  Qwen3-Reranker-0.6B llama-server instance (`--reranking --pooling rank`, :8083).
  `hybrid_search(..., rerank=True)` (default on) sends each candidate's top-3 nearest
  chunks (not just the single nearest — a lone administrative boilerplate chunk, e.g.
  "Author Contributions", can spuriously win on cosine distance and tank an otherwise
  top-ranked document) and takes the max reranker score. Fails soft to unmodified RRF
  order if the reranker is unreachable. `chunking.py` also gained
  `filter_administrative_sections()` to drop boilerplate sections before chunking at all,
  independent of whether reranking is on. See
  `decisions/second-brain-reranker-ab對照實驗結果-決策2.md` for the A/B results.

### Fixed

- Fixed two architecture debts from Phase B: (1) chunk-sync used to call the slow external
  late-chunking HTTP request while still holding the Postgres transaction/connection the
  caller had opened, which could starve unrelated concurrent MCP calls under load — split
  into a compute phase (all HTTP calls, no transaction open) and a write phase (pure SQL,
  short transaction) across `index_file`/`sync_all`/`sync_incremental`/`sync_chunks`.
  (2) `sync_chunks()` being unconditionally wired into `sync_index()` made that tool's
  runtime unbounded when a large backfill backlog was outstanding (observed: 90+ minutes,
  colliding with a concurrent backfill script on the same rows) — see the `limit`/
  `sync_chunks_tool()` addition above.
- Fixed `hybrid_search()` silently burying genuine chunk-only matches under
  thematically-similar notes-level noise: it used to score-merge the notes-level and
  chunk-level result list for each modality (keeping the higher raw cosine score per path)
  *before* RRF fusion. A whole-note embedding scores systematically higher on a
  thematically-clustered corpus than any single paragraph's embedding does for a narrow,
  back-half-only query, even when that paragraph is the one genuinely relevant chunk —
  confirmed live, a target's best chunk ranked #10 of 18 in isolation but fell to #46 of 52
  once merged by score, pushing it out of the candidate pool entirely. Fixed by feeding all
  four ranked lists (notes-BM25, chunks-BM25, notes-semantic, chunks-semantic) into RRF
  directly instead of pre-merging two of them by score — RRF is specifically designed to
  fuse heterogeneous ranked lists without comparable score scales, so pre-merging by score
  defeated its own purpose. `_merge_by_path_max_score` and its unit tests removed as
  dead/misleading once nothing called it for this any more.
- Fixed `semantic_keywords` extraction being silently dead for the sb vault specifically:
  `second-brain-remote`'s launchd plist never set `SB_LLM_BASE_URL` (the local-Gemma4
  backend `llm_cli.py` prefers), and neither the `claude` nor `gemini` CLI resolves on that
  service's `PATH` — so all three of `llm_cli.llm_text()`'s backends fell through to `None`
  on every note, with no error surfaced. Confirmed live: only 47 of 4251 sb notes had
  `semantic_keywords` set. `expand_semantic_keywords_tool()` additionally had its own,
  separate hard gate — `shutil.which("gemini")`, refusing to run at all without the literal
  `gemini` binary — left over from before the backend moved to `llm_cli`'s fallback chain;
  removed, since `_extract_semantic_keywords_via_gemini()` already fails soft. Backfilled
  all 4294 sb notes to 100% `semantic_keywords` coverage after the fix.
