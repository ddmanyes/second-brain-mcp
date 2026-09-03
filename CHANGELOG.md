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
