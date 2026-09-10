"""Pure validation and flat-frontmatter encoding for research article metadata."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
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


_ORCID_RE = re.compile(r"^(?:https?://orcid\.org/)?(\d{4}-\d{4}-\d{4}-\d{3}[\dXx])$")


def string_list(value: object, *, preserve_empty: bool = False) -> list[str]:
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
        if preserve_empty:
            result.append(text)
        elif text and text not in result:
            result.append(text)
    return result


def _aligned_author_values(
    authors_value: object,
    author_ids_value: object,
) -> tuple[list[str], list[str]]:
    """Normalise author/ID pairs without collapsing duplicate display names."""
    raw_authors = string_list(authors_value, preserve_empty=True)
    raw_ids = string_list(author_ids_value, preserve_empty=True)
    authors: list[str] = []
    author_ids: list[str] = []
    for index, author in enumerate(raw_authors):
        if not author:
            continue
        authors.append(author)
        author_ids.append(raw_ids[index] if index < len(raw_ids) else "")
    return authors, author_ids


def normalise_doi(value: object) -> str:
    doi = str(value or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.I)
    return doi.rstrip(".").lower()


def normalise_author_name(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    unmarked = "".join(char for char in decomposed if not unicodedata.combining(char))
    folded = unmarked.casefold().replace("’", "'")
    return " ".join("".join(char if char.isalnum() else " " for char in folded).split())


def _is_initials_token(value: str) -> bool:
    letters = "".join(char for char in value if char.isalpha())
    return bool(letters) and len(letters) <= 4 and (letters.isupper() or "." in value)


@dataclass(frozen=True)
class AuthorName:
    requested: str
    surname: str
    given_names: tuple[str, ...]
    aliases: tuple[str, ...]
    orcid: str = ""

    @classmethod
    def parse(cls, value: str) -> AuthorName:
        requested = " ".join(str(value or "").split())
        if not requested:
            raise ValueError("author is required")
        if match := _ORCID_RE.fullmatch(requested):
            return cls(requested, "", (), (), match.group(1).upper())

        if "," in requested:
            surname_raw, given_raw = requested.split(",", 1)
            surname_tokens = normalise_author_name(surname_raw).split()
            given_tokens = normalise_author_name(given_raw).split()
        else:
            raw_tokens = requested.split()
            tokens = normalise_author_name(requested).split()
            if len(tokens) == 1:
                surname_tokens, given_tokens = tokens, []
            elif len(raw_tokens) == 2 and _is_initials_token(raw_tokens[-1]):
                surname_tokens = normalise_author_name(raw_tokens[0]).split()
                given_tokens = normalise_author_name(raw_tokens[1]).split()
            else:
                surname_tokens, given_tokens = tokens[-1:], tokens[:-1]

        surname = " ".join(surname_tokens)
        initials = "".join(token[0] for token in given_tokens if token)
        values = (
            " ".join((*given_tokens, *surname_tokens)),
            " ".join((*surname_tokens, *given_tokens)),
            f"{surname} {initials}",
            f"{initials} {surname}",
        )
        aliases = tuple(dict.fromkeys(v.strip() for v in values if v.strip()))
        return cls(requested, surname, tuple(given_tokens), aliases)


def author_candidate_terms(query: str) -> tuple[str, ...]:
    identity = AuthorName.parse(query)
    return (identity.orcid,) if identity.orcid else identity.aliases


def author_search_text(authors: object, author_ids: object) -> str:
    terms: list[str] = []
    aligned_authors, aligned_ids = _aligned_author_values(authors, author_ids)
    for author in aligned_authors:
        try:
            terms.extend(AuthorName.parse(author).aliases)
        except ValueError:
            continue
    for value in aligned_ids:
        if not value:
            continue
        terms.extend((value.casefold(), normalise_author_name(value)))
    return " | ".join(dict.fromkeys(terms))


def match_indexed_author(
    query: str,
    authors_value: object,
    author_ids_value: object,
) -> tuple[str, str, bool] | None:
    """Return matched author, match type and ambiguity from structured fields only."""
    identity = AuthorName.parse(query)
    authors, author_ids = _aligned_author_values(authors_value, author_ids_value)
    if identity.orcid:
        for index, author_id in enumerate(author_ids):
            if author_id.upper() == identity.orcid:
                author = authors[index] if index < len(authors) else ""
                return author, "orcid", False
        return None

    for author in authors:
        candidate = AuthorName.parse(author)
        if not identity.given_names:
            if identity.surname == candidate.surname:
                return author, "surname", True
            continue
        if normalise_author_name(identity.requested) == normalise_author_name(author):
            return author, "full_name", False
        if set(identity.aliases) & set(candidate.aliases):
            return author, "alias", False
    return None


def article_result(row: dict, *, author: str = "") -> dict | None:
    authors, author_ids = _aligned_author_values(
        row.get("authors_json"), row.get("author_ids_json")
    )
    matched_author = ""
    match_type = "identifier"
    ambiguous = False
    if author:
        match = match_indexed_author(
            author,
            authors,
            author_ids,
        )
        if match is None:
            return None
        matched_author, match_type, ambiguous = match
    return {
        "path": row.get("path") or "",
        "title": row.get("title") or "",
        "authors": authors,
        "author_ids": author_ids,
        "matched_author": matched_author,
        "match_type": match_type,
        "ambiguous": ambiguous,
        "doi": row.get("doi") or "",
        "pmid": row.get("pmid") or "",
        "pmcid": row.get("pmcid") or "",
        "journal": row.get("journal") or "",
        "publication_year": row.get("publication_year"),
        "canonical_url": row.get("canonical_url") or "",
    }


def normalise_bibliographic_metadata(metadata: dict | None) -> dict:
    """Whitelist and normalise values that may be written to frontmatter."""
    raw = metadata or {}
    result: dict = {}
    authors, author_ids = _aligned_author_values(
        raw.get("authors"), raw.get("author_ids")
    )
    if authors:
        result["authors"] = authors
        if "author_ids" in raw or any(author_ids):
            result["author_ids"] = author_ids

    if doi := normalise_doi(raw.get("doi")):
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
    fields = bibliographic_fields(metadata)
    return "".join(f"{key}: {value}\n" for key, value in fields.items())


def bibliographic_fields(metadata: dict | None) -> dict[str, str]:
    """Return validated metadata as frontmatter-ready scalar strings."""
    normalised = normalise_bibliographic_metadata(metadata)
    fields: dict[str, str] = {}
    for key in FIELDS:
        if key not in normalised:
            continue
        value = normalised[key]
        if isinstance(value, (list, str)):
            rendered = json.dumps(value, ensure_ascii=False)
        else:
            rendered = str(value)
        fields[key] = rendered
    return fields
