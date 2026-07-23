"""snippets.py — verbatim source-sentence extraction for precise localization.

Ported from BAR's vault_search (keyword_snippet + noise filtering) so that a query lands on the
EXACT source sentence in a note, mechanically extracted (never rewritten) to avoid fabricating
what a paper did not say. Used by the `search_snippets` MCP tool on top of hybrid_search.

Discipline (from BAR / INDRA / SemRep prior art):
  - strip the References section (cited-paper titles are not this paper's own claims)
  - skip author/institution/acknowledgment/data-availability boilerplate (false hits)
  - return the verbatim sentence, not an LLM paraphrase
"""
from __future__ import annotations

import re

_FRONTMATTER = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)
_MD_NOISE = re.compile(r"[*`~]+")
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\[\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_ESC = re.compile(r"\\([()\[\]])")
_REFERENCES = re.compile(
    r"^#{1,6}\s*\**\s*(references|bibliography|reference list|參考文獻|works cited)\b",
    re.IGNORECASE | re.MULTILINE,
)
_METADATA_NOISE = re.compile(
    r"@|Tel:|Fax:|correspondence should be addressed|orcid|"
    r"\bDivision of\b|\bDepartment of\b|\bInstitute\b|"
    r"\bcore facilit|were prepared by the|sequencing librar|"
    r"deposited (to|in)|proteomexchange|data availabilit|accession (number|code|no)|"
    r"acknowledg|conflict of interest|author contribution",
    re.IGNORECASE,
)
_SENT_SPLIT = re.compile(r"(?:[。！？]+|[.!?]+(?=\s)|\n+)\s*")
_MD_LEADING = re.compile(r"^[#>*+\-\s\t]+")
_WORD = re.compile(r"[A-Za-z0-9一-鿿][A-Za-z0-9\-一-鿿]{2,}")


def strip_frontmatter(text: str) -> str:
    return _FRONTMATTER.sub("", text, count=1)


def strip_references(body: str) -> str:
    m = _REFERENCES.search(body)
    return body[: m.start()] if m else body


def _clean_inline(text: str) -> str:
    text = _MD_IMG.sub("", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = _ESC.sub(r"\1", text)
    text = _MD_NOISE.sub("", text)
    return text.strip()


def _is_metadata_noise(seg: str) -> bool:
    return bool(_METADATA_NOISE.search(seg))


def _snippet_for_term(body: str, term: str, title: str, max_len: int) -> str:
    kw = term.lower()
    title_norm = title.lower()
    for raw in _SENT_SPLIT.split(body):
        if _is_metadata_noise(raw):
            continue
        seg = _clean_inline(_MD_LEADING.sub("", raw))
        if not seg or kw not in seg.lower():
            continue
        seg = re.sub(r"\s+", " ", seg)
        if seg.lower() == title_norm:
            continue
        if len(seg) <= max_len:
            return seg
        idx = seg.lower().find(kw)
        start = max(0, idx - max_len // 2)
        end = start + max_len
        return f"{'…' if start > 0 else ''}{seg[start:end]}{'…' if end < len(seg) else ''}"
    return ""


def best_snippet(raw_note: str, title: str, query: str, *, max_len: int = 220) -> str:
    """Verbatim sentence best matching the query. Tries the full query, then its salient
    terms (longest first). Returns '' if nothing substantive matches (caller can skip)."""
    body = strip_references(strip_frontmatter(raw_note))
    # 1) whole query as a phrase
    s = _snippet_for_term(body, query, title, max_len)
    if s:
        return s
    # 2) salient terms, longest first (so 'fibrosis' beats 'the')
    terms = sorted({w for w in _WORD.findall(query)}, key=len, reverse=True)
    for t in terms:
        s = _snippet_for_term(body, t, title, max_len)
        if s:
            return s
    return ""
