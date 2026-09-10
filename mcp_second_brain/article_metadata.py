"""Pure validation and flat-frontmatter encoding for research article metadata."""

from __future__ import annotations

import json
import re
from datetime import date

FIELDS = (
    "authors",
    "author_ids",
    "doi",
    "pmid",
    "pmcid",
    "journal",
    "publication_year",
    "canonical_url",
)


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            decoded = value.split(",")
        values = decoded if isinstance(decoded, list) else [decoded]
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        return []
    result: list[str] = []
    for item in values:
        text = " ".join(str(item or "").split())
        if text and text not in result:
            result.append(text)
    return result


def normalise_bibliographic_metadata(metadata: dict | None) -> dict:
    """Whitelist and normalise values that may be written to frontmatter."""
    raw = metadata or {}
    result: dict = {}
    for key in ("authors", "author_ids"):
        values = _string_list(raw.get(key))
        if values:
            result[key] = values

    doi = str(raw.get("doi") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    if doi := doi.rstrip(".").lower():
        result["doi"] = doi

    pmid = "".join(c for c in str(raw.get("pmid") or "") if c.isdigit())
    if pmid:
        result["pmid"] = pmid

    pmcid = str(raw.get("pmcid") or "").strip().upper()
    if pmcid:
        result["pmcid"] = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"

    journal = " ".join(str(raw.get("journal") or "").split())
    if journal:
        result["journal"] = journal

    try:
        year = int(str(raw.get("publication_year") or "")[:4])
    except (TypeError, ValueError):
        year = 0
    if 1000 <= year <= date.today().year + 1:
        result["publication_year"] = year

    canonical_url = str(raw.get("canonical_url") or "").strip()
    if canonical_url.startswith(("https://", "http://")):
        result["canonical_url"] = canonical_url
    return result


def bibliographic_frontmatter(metadata: dict | None) -> str:
    """Return YAML-compatible one-line fields, including the final newline."""
    normalised = normalise_bibliographic_metadata(metadata)
    lines = []
    for key in FIELDS:
        if key not in normalised:
            continue
        value = normalised[key]
        if isinstance(value, (list, str)):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            rendered = str(value)
        lines.append(f"{key}: {rendered}")
    return "".join(f"{line}\n" for line in lines)
