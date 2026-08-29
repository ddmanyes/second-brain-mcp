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
