# Changelog

All notable source-tree changes are recorded here.

## Unreleased

### Added

- Added the read-only `audit_article_records` MCP tool with bounded parameters,
  structured output, and an equivalent text fallback for older clients.
- Added article housekeeping checks for required frontmatter, broken wikilinks,
  exact duplicate candidates, overdue inbox records, and social-source freshness.
- Added actionable warnings for unreadable vaults, invalid source state, and invalid
  parameter bounds without performing automatic merge, archive, or deletion.

### Fixed

- Fixed note snapshot rendering from async MCP requests by running the synchronous
  Playwright renderer in a worker thread.
- Aligned article-audit Markdown exclusions with the indexer so `.obsidian`, `.claude`,
  and `templates` do not produce false-positive `index_gap` values.
- Replaced the stalled 30-minute launchd interval with calendar triggers at minute 0
  and 30 in the pg-sync deployment template.
