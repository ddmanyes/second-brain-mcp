"""Plan and safely apply structured author metadata backfills.

The planner only sends identifiers already present in article frontmatter to the
metadata provider.  If no stable identifier exists it permits an exact-title
lookup, but never scans article prose or references for identity evidence.
Applying a plan is a separate bounded operation protected by content and body
hashes so a stale plan cannot overwrite intervening edits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import requests

from .article_metadata import (
    bibliographic_fields,
    normalise_bibliographic_metadata,
    normalise_doi,
    string_list,
)
from .frontmatter import set_fields
from .note_row import FRONTMATTER_RE, parse_frontmatter

MAX_NOTES = 20
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
_EXCLUDED_PARTS = {".obsidian", ".claude", "templates", "40-archive"}


class MetadataProvider(Protocol):
    def lookup(self, **identity: str) -> dict | None: ...


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _body(text: str) -> str:
    return FRONTMATTER_RE.sub("", text, count=1)


def _normalise_title(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    unmarked = "".join(char for char in text if not unicodedata.combining(char))
    return " ".join(
        "".join(char if char.isalnum() else " " for char in unmarked.casefold()).split()
    )


def _is_article(relative: str, frontmatter: dict[str, str]) -> bool:
    note_type = frontmatter.get("type", "").strip().lower()
    if note_type == "sync_state":
        return False
    return (
        relative.startswith(("20-areas/research/", "30-resources/"))
        or note_type in {"paper", "research"}
        or bool(frontmatter.get("source", "").strip())
    )


def _safe_markdown_files(root: Path, paths: list[str] | None) -> list[Path]:
    if paths is None:
        candidates = root.rglob("*.md")
    else:
        candidates = (root / relative for relative in paths)
    safe: list[Path] = []
    for candidate in candidates:
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if _EXCLUDED_PARTS.intersection(relative.parts):
            continue
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_relative_to(root) and resolved.is_file() and resolved.suffix == ".md":
            safe.append(resolved)
    return sorted(set(safe), key=lambda path: path.relative_to(root).as_posix())


def _identity(frontmatter: dict[str, str]) -> dict[str, str]:
    doi = normalise_doi(frontmatter.get("doi"))
    if _DOI_RE.fullmatch(doi):
        return {"doi": doi}
    if pmid := "".join(char for char in frontmatter.get("pmid", "") if char.isdigit()):
        return {"pmid": pmid}
    pmcid = frontmatter.get("pmcid", "").strip().upper()
    if pmcid:
        pmcid = pmcid if pmcid.startswith("PMC") else f"PMC{pmcid}"
    if pmcid.startswith("PMC") and pmcid[3:].isdigit():
        return {"pmcid": pmcid}

    source = frontmatter.get("source", "").strip()
    if match := _DOI_RE.search(source):
        return {"doi": normalise_doi(match.group(0))}
    if source:
        parsed = urlsplit(source)
        parts = [part for part in parsed.path.split("/") if part]
        host = (parsed.hostname or "").casefold()
        is_ncbi = host == "ncbi.nlm.nih.gov" or host.endswith(".ncbi.nlm.nih.gov")
        if host == "pubmed.ncbi.nlm.nih.gov" and parts and parts[0].isdigit():
            return {"pmid": parts[0]}
        if is_ncbi:
            for part in parts:
                if part.upper().startswith("PMC") and part[3:].isdigit():
                    return {"pmcid": part.upper()}

    title = frontmatter.get("title", "").strip()
    return {"title": title} if title else {}


def _exact_match(identity: dict[str, str], result: dict) -> tuple[bool, str]:
    key, requested = next(iter(identity.items()))
    if key == "title":
        return _normalise_title(requested) == _normalise_title(result.get("title")), "title_exact"
    if key == "doi":
        matched = normalise_doi(result.get("doi")) == requested
    elif key == "pmid":
        matched = "".join(char for char in str(result.get("pmid") or "") if char.isdigit()) == requested
    else:
        value = str(result.get("pmcid") or "").strip().upper()
        value = value if value.startswith("PMC") else f"PMC{value}" if value else ""
        matched = value == requested
    return matched, f"{key}_exact"


def plan_backfill(
    vault: Path | str,
    *,
    provider: MetadataProvider,
    paths: list[str] | None = None,
    limit: int = MAX_NOTES,
) -> dict:
    """Create a read-only manifest for article notes missing structured authors."""
    if not 1 <= limit <= MAX_NOTES:
        raise ValueError(f"limit must be between 1 and {MAX_NOTES}")
    root = Path(vault).expanduser().resolve()
    entries: list[dict] = []
    review: list[dict] = []
    eligible = 0
    for path in _safe_markdown_files(root, paths):
        text = path.read_text(encoding="utf-8", errors="strict")
        fm = parse_frontmatter(text)
        relative = path.relative_to(root).as_posix()
        if not _is_article(relative, fm) or string_list(fm.get("authors")):
            continue
        if eligible >= limit:
            break
        eligible += 1

        identity = _identity(fm)
        if not identity:
            review.append({"path": relative, "reason": "missing_identity"})
            continue
        try:
            result = provider.lookup(**identity)
        except Exception as exc:
            review.append(
                {"path": relative, "reason": f"provider_error:{type(exc).__name__}"}
            )
            continue
        if not result:
            review.append({"path": relative, "reason": "not_found"})
            continue
        exact, basis = _exact_match(identity, result)
        if not exact:
            reason = "title_mismatch" if "title" in identity else "identifier_mismatch"
            review.append({"path": relative, "reason": reason})
            continue
        metadata = normalise_bibliographic_metadata(result)
        if not metadata.get("authors"):
            review.append({"path": relative, "reason": "authors_missing"})
            continue
        encoded = text.encode()
        entries.append(
            {
                "path": relative,
                "match_basis": basis,
                "confidence": "deterministic",
                "content_sha256": _sha256_bytes(encoded),
                "body_sha256": _sha256_bytes(_body(text).encode()),
                "metadata": metadata,
            }
        )

    return {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "vault": str(root),
        "summary": {
            "eligible": eligible,
            "planned": len(entries),
            "needs_review": len(review),
        },
        "entries": entries,
        "review": review,
    }


def _atomic_write(path: Path, content: str, mode: int) -> None:
    temporary = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".part",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def apply_backfill(
    vault: Path | str,
    manifest: dict,
    *,
    indexer=None,
    limit: int = MAX_NOTES,
) -> dict:
    """Apply one previously reviewed manifest without changing article bodies."""
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("manifest entries must be a list")
    if not 1 <= limit <= MAX_NOTES or len(entries) > limit:
        raise ValueError(f"a manifest may contain at most {MAX_NOTES} entries")
    if manifest.get("version") != 1:
        raise ValueError("unsupported manifest version")
    root = Path(vault).expanduser().resolve()
    planned_root = Path(str(manifest.get("vault") or "")).expanduser().resolve()
    if planned_root != root:
        raise ValueError("manifest was created for a different vault")
    applied = 0
    skipped = 0
    errors: list[dict[str, str]] = []

    for entry in entries:
        relative = str(entry.get("path") or "")
        if entry.get("confidence") != "deterministic":
            skipped += 1
            errors.append({"path": relative, "reason": "confidence_not_deterministic"})
            continue
        try:
            path = (root / relative).resolve(strict=True)
        except (OSError, RuntimeError):
            skipped += 1
            errors.append({"path": relative, "reason": "missing_file"})
            continue
        if not path.is_relative_to(root) or path.suffix != ".md":
            skipped += 1
            errors.append({"path": relative, "reason": "unsafe_path"})
            continue
        original = path.read_text(encoding="utf-8")
        if _sha256_bytes(original.encode()) != entry.get("content_sha256"):
            skipped += 1
            errors.append({"path": relative, "reason": "content_changed"})
            continue
        fields = bibliographic_fields(entry.get("metadata"))
        if "authors" not in fields:
            skipped += 1
            errors.append({"path": relative, "reason": "authors_missing"})
            continue
        updated = set_fields(original, fields)
        if _sha256_bytes(_body(updated).encode()) != entry.get("body_sha256"):
            skipped += 1
            errors.append({"path": relative, "reason": "body_changed"})
            continue
        _atomic_write(path, updated, path.stat().st_mode & 0o777)
        applied += 1
        if indexer is not None:
            try:
                indexer(path)
            except Exception as exc:  # the file is canonical; surface index drift explicitly
                errors.append({"path": relative, "reason": f"index_failed:{type(exc).__name__}"})

    return {"applied": applied, "skipped": skipped, "errors": errors}


def write_manifest(destination: Path | str, manifest: dict) -> None:
    """Write a plan atomically; this never changes a vault note."""
    path = Path(destination).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, path)


class EuropePmcProvider:
    """Exact-record metadata lookup against the Europe PMC search API."""

    endpoint = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"

    def __init__(self, *, timeout: float = 20.0):
        self.timeout = timeout

    def lookup(self, **identity: str) -> dict | None:
        key, value = next(iter(identity.items()))
        field = {"doi": "DOI", "pmid": "EXT_ID", "pmcid": "PMCID", "title": "TITLE"}[key]
        response = requests.get(
            self.endpoint,
            params={"query": f'{field}:"{value}"', "format": "json", "pageSize": 5},
            timeout=self.timeout,
        )
        response.raise_for_status()
        rows = response.json().get("resultList", {}).get("result", [])
        matches = [self._metadata(row) for row in rows]
        matches = [candidate for candidate in matches if _exact_match(identity, candidate)[0]]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _metadata(row: dict) -> dict:
        author_rows = row.get("authorList", {}).get("author", []) or []
        authors: list[str] = []
        author_ids: list[str] = []
        for author in author_rows:
            full_name = str(author.get("fullName") or "").strip()
            if not full_name:
                continue
            raw_id = author.get("authorId") or ""
            if isinstance(raw_id, dict):
                id_type = str(raw_id.get("type") or "").casefold()
                raw_id = raw_id.get("value") or raw_id.get("id") or ""
                if id_type and id_type != "orcid":
                    raw_id = ""
            author_id = str(raw_id).removeprefix("https://orcid.org/").upper()
            if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", author_id):
                author_id = ""
            authors.append(full_name)
            author_ids.append(author_id)
        return {
            "title": row.get("title") or "",
            "authors": authors,
            "author_ids": author_ids[: len(authors)],
            "doi": row.get("doi") or "",
            "pmid": row.get("pmid") or row.get("id") or "",
            "pmcid": row.get("pmcid") or "",
            "journal": row.get("journalTitle") or "",
            "publication_year": row.get("pubYear") or "",
            "canonical_url": (
                f"https://europepmc.org/article/MED/{row.get('pmid') or row.get('id')}"
                if row.get("pmid") or row.get("id")
                else ""
            ),
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", required=True, type=Path)
    parser.add_argument("--limit", type=int, default=MAX_NOTES)
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-index", action="store_true")
    args = parser.parse_args(argv)

    if args.apply:
        if args.manifest is None:
            parser.error("--apply requires --manifest")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        indexer = None
        if not args.no_index:
            from .store import get_store

            store = get_store()

            def indexer(path: Path) -> None:
                store.index_file(args.vault.resolve(), path)
        print(json.dumps(apply_backfill(args.vault, manifest, indexer=indexer), ensure_ascii=False))
        return 0

    if args.out is None:
        parser.error("planning requires --out")
    manifest = plan_backfill(
        args.vault,
        provider=EuropePmcProvider(),
        paths=args.paths,
        limit=args.limit,
    )
    write_manifest(args.out, manifest)
    print(json.dumps(manifest["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
