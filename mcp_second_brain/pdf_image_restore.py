"""Fail-closed restoration of missing legacy PDF image assets."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path


EmbeddedImage = dict[str, object]
Extractor = Callable[[Path], list[EmbeddedImage]]
_SOURCE_PDF_RE = re.compile(r"^\|\s*\*\*Source PDF\*\*\s*\|\s*(.*?)\s*\|\s*$", re.MULTILINE)


def _extract_legacy_sequence(pdf_path: Path) -> list[EmbeddedImage]:
    """Reproduce the historical PyMuPDF embedded-image occurrence order."""
    import fitz

    images: list[EmbeddedImage] = []
    with fitz.open(pdf_path) as document:
        for page_number, page in enumerate(document):
            for ordinal, item in enumerate(page.get_images(full=True)):
                xref = item[0]
                extracted = document.extract_image(xref)
                if extracted["width"] <= 100 or extracted["height"] <= 100:
                    continue
                images.append(
                    {
                        "page": page_number,
                        "ordinal": ordinal,
                        "xref": xref,
                        "ext": str(extracted["ext"]).lower(),
                        "data": extracted["image"],
                    }
                )
    return images


def _source_pdf(note_file: Path, override: str | None = None) -> Path | None:
    if override is None:
        match = _SOURCE_PDF_RE.search(note_file.read_text(encoding="utf-8"))
        if not match:
            return None
        raw_source = match.group(1)
    else:
        raw_source = override
    source = Path(raw_source).expanduser().resolve()
    if source.suffix.lower() != ".pdf" or not source.is_file():
        return None
    return source


def _same_bytes(path: Path, data: bytes) -> bool:
    return hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(data).digest()


def _error(note_path: str, reason: str, **detail: object) -> dict:
    return {"note_path": note_path, "action": "validation_failed", "reason": reason, **detail}


def restore_missing_pdf_images(
    note_paths: list[str],
    vault: Path,
    store,
    *,
    dry_run: bool = True,
    note_limit: int = 20,
    image_limit: int = 20,
    source_pdfs: list[str] | None = None,
    extract: Extractor = _extract_legacy_sequence,
) -> dict:
    """Restore missing PDF image files only after the full sequence matches.

    The historical extractor emitted every embedded image occurrence whose width
    and height were both greater than 100 pixels. Before any write, this function
    requires the reconstructed sequence to match the database row count, contiguous
    figure indices, filename extensions, and every still-existing file byte-for-byte.
    """
    if not note_paths:
        raise ValueError("at least one note path is required")
    if len(note_paths) > 20:
        raise ValueError("at most 20 note paths are allowed")
    if not 1 <= note_limit <= 20:
        raise ValueError("note_limit must be between 1 and 20")
    if not 1 <= image_limit <= 20:
        raise ValueError("image_limit must be between 1 and 20")
    if source_pdfs is not None and len(source_pdfs) != len(note_paths):
        raise ValueError("source_pdfs must align one-to-one with note_paths")

    vault = vault.expanduser().resolve()
    selected = list(dict.fromkeys(note_paths))[:note_limit]
    override_by_note = (
        dict(zip(note_paths, source_pdfs, strict=True)) if source_pdfs is not None else {}
    )
    plans: list[dict] = []
    errors: list[dict] = []

    for note_path in selected:
        note_file = (vault / note_path).resolve()
        if not note_file.is_relative_to(vault) or not note_file.is_file():
            errors.append(_error(note_path, "missing_or_unsafe_note"))
            continue
        pdf_path = _source_pdf(note_file, override_by_note.get(note_path))
        if pdf_path is None:
            errors.append(_error(note_path, "missing_source_pdf"))
            continue

        rows = sorted(store.get_figures_for_note(note_path), key=lambda row: row["fig_index"])
        sequence = extract(pdf_path)
        if len(sequence) != len(rows):
            errors.append(
                _error(
                    note_path,
                    "sequence_row_count_mismatch",
                    sequence_count=len(sequence),
                    row_count=len(rows),
                )
            )
            continue
        if [row["fig_index"] for row in rows] != list(range(len(rows))):
            errors.append(_error(note_path, "non_contiguous_figure_indices"))
            continue

        note_plans: list[dict] = []
        for row, embedded in zip(rows, sequence, strict=True):
            data = embedded.get("data")
            if not isinstance(data, bytes):
                errors.append(_error(note_path, "invalid_extracted_image"))
                break
            destination = Path(str(row.get("local_path") or "")).expanduser().resolve()
            if not destination.is_relative_to(vault / "figures"):
                errors.append(
                    _error(note_path, "unsafe_local_path", fig_index=row["fig_index"])
                )
                break
            expected_ext = f".{embedded['ext']}"
            if destination.suffix.lower() != expected_ext:
                errors.append(
                    _error(
                        note_path,
                        "extension_mismatch",
                        fig_index=row["fig_index"],
                        expected=expected_ext,
                        actual=destination.suffix.lower(),
                    )
                )
                break
            identity = {
                "note_path": note_path,
                "fig_index": row["fig_index"],
                "local_path": str(destination),
                "pdf_path": str(pdf_path),
                "page": embedded["page"],
                "ordinal": embedded["ordinal"],
                "xref": embedded["xref"],
            }
            if destination.exists():
                if not destination.is_file() or not _same_bytes(destination, data):
                    errors.append(_error(**identity, reason="existing_file_mismatch"))
                    break
                continue
            note_plans.append({**identity, "data": data})
        else:
            plans.extend(note_plans)

    if errors:
        return {
            "dry_run": dry_run,
            "note_paths": selected,
            "items": errors,
            "summary": {
                "candidates": len(plans),
                "planned": 0,
                "applied": 0,
                "failed": len(errors),
                "halted": True,
            },
        }

    bounded = plans[:image_limit]
    items: list[dict] = []
    applied = failed = 0
    halted = False
    for plan in bounded:
        data = plan.pop("data")
        if dry_run:
            items.append({**plan, "action": "would_restore"})
            continue
        destination = Path(plan["local_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            failed += 1
            halted = True
            items.append({**plan, "action": "write_failed", "error": type(exc).__name__})
            break
        applied += 1
        items.append({**plan, "action": "restored"})

    return {
        "dry_run": dry_run,
        "note_paths": selected,
        "items": items,
        "summary": {
            "candidates": len(plans),
            "planned": len(bounded) if dry_run else 0,
            "applied": applied,
            "failed": failed,
            "halted": halted,
        },
    }
