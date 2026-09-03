# AGENTS.md — Personal Rules Layer (literature vault + knowledge graph)

> **What this file is.** `get_agent_instructions()` returns two layers: the base manual
> (this package's `AGENTS.md`) followed by the personal layer, which is this file, read from
> your vault root. This sample is for a vault dedicated to **scientific literature** that
> also maintains a knowledge graph. For a general-purpose vault see
> `agents-personal-layer-general-sample.md`.
>
> **Put new personal rules here, never in the base manual.**
>
> **Last updated:** {{date}}

---

## Vault identity

{{VAULT_NAME}} is a literature vault: papers, preprints, and manuscripts, plus a causal
knowledge graph (`.graph/statements.jsonl`) built from them.

**State the boundary against your other vaults.** Running two vaults off one package is
common and mis-filing is the usual failure:

> {{VAULT_NAME}} holds literature and the graph; {{OTHER_VAULT}} holds projects,
> architecture, and records. Anything paper-shaped belongs here, not there.

---

## Paper note naming

Do not keep the long kebab-case title `new_note` derives from a paper title. Use:

```text
{YYYY}_{FirstAuthorLastName}_{ShortTitle}.md
```

- `YYYY` — year of online publication
- `FirstAuthorLastName` — PascalCase
- `ShortTitle` — a system name or keyword in PascalCase, **not** the full title
- Location: `20-areas/research/`

Example: `2026_Huang_MacrophageHairFollicle.md`

| Condition | Location | Naming |
| --- | --- | --- |
| Has DOI / journal / identified first author | `20-areas/research/` | `{YYYY}_{Author}_{ShortTitle}.md` |
| No academic source (tool docs, review resources) | `30-resources/` | `{kebab-slug}.md` |
| Curated paper set for one case (citations + checklist) | `30-resources/` | `{case}-literature-index.md` |
| Unsure | `00-inbox/` | `{YYYY-MM-DD}-{topic}.md` (max 7 days here) |

**If a central paper has no findable public PMID/DOI**, assume it may be an unpublished
manuscript and ask the user to confirm publication status. Do not assume it is published.

---

## Retrieval methodology

Single lookups are covered by the base manual's §B. Use the flow below when the result
feeds a downstream decision, or when you are checking an existing analysis against
the literature.

**A. Standard search**

1. `search_notes` / `search_grouped` over several keyword sets (include non-English terms
   if your corpus has them) to build a candidate pool.
2. Expand along `related:` / `semantic_keywords` from the anchor notes.
3. For a secondary detail inside a very long note (60K+ characters), semantic ranking can
   dilute it — grep the vault directly when needed.
4. `query_graph` — **pass `entity` explicitly**. Relying on auto-extraction has been
   observed to miss key papers.
5. `litnet_answer` last. It is the synthesis step, not a retrieval step.

**B. Literature ↔ data cross-validation loop**

1. Screen each candidate on three criteria (specific marker + cell type + direction).
   A paper that fails screening gets a one-line summary, nothing more.
2. Confirm publication status before relying on it.
3. Read closely → turn each claim into a checkable task → verify with your analysis tools →
   record support / refutation / uncertainty **honestly**.
4. On a gap: not enough literature → widen the candidate pool; flawed analysis method →
   fix and re-run. Do not go looking for new papers to paper over a method problem.
5. Ask the user before each additional round. On a **final** stop (not a pause), produce a
   standalone summary report separate from the step-by-step log.

---

## Graph ingestion — the two-stage closure

A new note does **not** reach the knowledge graph by being saved. Two independent stages
must each be triggered; assuming "saved" means "in the graph" is the usual mistake:

1. **Note → search index.** `new_note` does not index immediately; call `sync_index()`.
   `save_article` indexes on save and skips this trap.
2. **Note's causal claims → graph.** Relations in the note body are not extracted
   automatically. Run your extract → judge → promote pipeline to write into
   `.graph/statements.jsonl`.

Document your pipeline's scripts, models, and **cost warnings** in the pipeline repo and
link to it from here rather than duplicating the table in two places that will drift.

---

## Editing discipline

1. **Rewriting existing content**: `read_note` → `update_note` (full overwrite) →
   immediately `read_note` again to verify the frontmatter, especially `related:`.
   If it is corrupted, do not call `update_note` again to repair it — edit on disk.
2. **Create through the tool**: always `new_note`, then `sync_index()`.
3. **PDF → markdown** must use your real extractor; silently falling back to a weaker
   converter has been a genuine bug.

---

## Key vault files

- `memory/goals.md` — current priorities; read by `get_context()`
- `.graph/statements.jsonl` — the canonical graph (judged, evidence-backed edges).
  **Hard rule:** literature-derived edges go here only. Never mix them into a
  hypothesis-generation pool, which holds machine-generated claims, not evidence.
