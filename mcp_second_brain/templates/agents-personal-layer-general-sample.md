# AGENTS.md — Personal Rules Layer (general-purpose vault)

> **What this file is.** `get_agent_instructions()` returns two layers: the base manual
> (this package's `AGENTS.md` — tools, note types, security rules, SOPs) followed by the
> personal layer, which is this file, read from your vault root. The base layer is shared
> by every vault; this file is where one vault's own conventions go.
>
> **Put new personal rules here, never in the base manual** — the base manual ships with
> the package and your edits would be lost on upgrade.
>
> Copy this file to `<your-vault>/AGENTS.md` and adapt. See
> `agents-personal-layer-literature-sample.md` for a vault that also runs a knowledge graph.
>
> **Last updated:** {{date}}

---

## Vault identity

{{VAULT_NAME}} holds general knowledge: projects, architecture notes, decisions, test
records, personal areas.

If you run **more than one vault off this same package**, say so here explicitly and state
the boundary. Two vaults sharing one codebase is the setup this file is written for, and
the most common agent mistake is filing into the wrong one. Example boundary statement:

> {{VAULT_NAME}} holds projects/architecture/records; {{OTHER_VAULT}} holds literature.
> Anything paper-shaped goes to {{OTHER_VAULT}}, not here.

---

## Filing decision tree

Route external content by whether it has a clear academic source:

| Condition | Location | Naming |
| --- | --- | --- |
| Has DOI / journal / identified first author | `20-areas/research/` | `{YYYY}_{Author}_{ShortTitle}.md` |
| No academic source (official docs, tutorials, tool notes) | `30-resources/` | `{kebab-slug}.md` |
| {{DOMAIN_SPECIFIC_TYPE}} (e.g. per-ticker reports, meeting notes) | `{{DOMAIN_FOLDER}}` | `{{DOMAIN_NAMING}}` |
| Unsure | `00-inbox/` | `{YYYY-MM-DD}-{topic}.md` (max 7 days here) |

Check your project registry for the canonical slug before filing project notes.

---

## Editing discipline

1. **Partial updates, not whole-file rewrites** — touch only the section that changes.
   Prefer `append_to_note`; reach for `read_note` → `update_note` only for a major rewrite.
2. **Leave unrelated content alone** — do not reorder frontmatter keys, reformat tables,
   or reflow paragraphs you were not asked to change.
3. **Create through the tool** — new notes always via `new_note` so the template, folder
   routing, and indexing apply. Note that `new_note` does **not** index immediately: call
   `sync_index()` afterwards or the note is not searchable. `save_article` indexes on save.
4. **Verify after writing** — re-read the note and confirm the frontmatter survived,
   especially list-valued keys like `related:`. If a write corrupts frontmatter, fix it on
   disk rather than calling the same tool again.

---

## Splitting an over-long note

A file-count rule ("more than N notes in a project → make a subdirectory") never fires for
a flat project whose content all accumulates in **one** file via repeated appends — the count
stays at 1 until a read finally fails on token limits.

**Split when any of these is true** (do not wait for a read to fail):

1. More than ~10 dated `##` sections in one flat note.
2. The note is already ~50K characters when you read it before appending.
3. It covers several distinct topics or work phases rather than one continuous thread.

**How**: create `10-projects/{slug}/{phases,docs}/`, split by date or topic, reduce the
original to a short overview plus links, and update the registry entry to the new structure.
Splitting is a deliberate restructure and is the documented exception to "partial updates".

---

## Key vault files

- `memory/goals.md` — current priorities; read by `get_context()`
- `memory/rules.md` — active rules injected into sessions
- {{ADD_YOUR_OWN}}
