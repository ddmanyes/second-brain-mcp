"""Bounded, resumable figure OCR/description backfill through a local VLM."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from .figures import analyse_figure

Analysis = dict | None


def _has_text_proxy(row: dict) -> bool:
    return any(
        str(row.get(field) or "").strip()
        for field in ("ocr_text", "description", "caption")
    )


def _local_image(vault: Path, row: dict) -> tuple[Path | None, str | None]:
    raw = Path(str(row.get("local_path") or "")).expanduser()
    candidate = raw if raw.is_absolute() else vault / raw
    resolved = candidate.resolve()
    if not resolved.is_relative_to(vault):
        return None, "unsafe_local_path"
    if not resolved.is_file():
        return None, "missing_local_file"
    return resolved, None


def _validate_limits(note_paths: list[str], note_limit: int, image_limit: int) -> None:
    if not note_paths:
        raise ValueError("at least one note path is required")
    if len(note_paths) > 20:
        raise ValueError("at most 20 note paths are allowed")
    if not 1 <= note_limit <= 20:
        raise ValueError("note_limit must be between 1 and 20")
    if not 1 <= image_limit <= 20:
        raise ValueError("image_limit must be between 1 and 20")


def backfill_figure_text(
    note_paths: list[str],
    vault: Path,
    store,
    *,
    dry_run: bool = True,
    note_limit: int = 20,
    image_limit: int = 20,
    analyse: Callable[[Path, str], Analysis] = analyse_figure,
) -> dict:
    """Analyse at most ``image_limit`` existing empty figure rows.

    Each successful image is committed independently through ``store``. Model
    failures leave the original row untouched and processing continues; a store
    failure halts the batch because commit state is then uncertain. Re-running
    is idempotent because rows with any text proxy are skipped.
    """
    _validate_limits(note_paths, note_limit, image_limit)
    if os.environ.get("SB_VISION_BACKEND", "").strip().lower() != "local-only":
        raise ValueError("figure text backfill requires SB_VISION_BACKEND=local-only")

    vault = vault.expanduser().resolve()
    selected_notes = list(dict.fromkeys(note_paths))[:note_limit]
    rows: list[dict] = []
    items: list[dict] = []
    for note_path in selected_notes:
        note_file = (vault / note_path).resolve()
        if not note_file.is_relative_to(vault):
            raise ValueError(f"note path is outside vault: {note_path}")
        for row in store.get_figures_for_note(note_path):
            if not _has_text_proxy(row):
                rows.append(row)

    attempted = applied = failed = manual = 0
    halted = False
    for row in rows:
        image, reason = _local_image(vault, row)
        identity = {
            "note_path": row["note_path"],
            "fig_index": row["fig_index"],
            "local_path": row.get("local_path", ""),
        }
        if reason:
            manual += 1
            items.append({**identity, "action": "manual_queue", "reason": reason})
            continue
        if attempted >= image_limit:
            continue
        attempted += 1
        if dry_run:
            items.append({**identity, "action": "would_analyse"})
            continue

        analysis = analyse(image, str(row.get("caption") or ""))
        if analysis is None or not any(
            str(analysis.get(field) or "").strip()
            for field in ("ocr_text", "description")
        ):
            failed += 1
            items.append({**identity, "action": "model_failed"})
            continue

        updated = {
            "note_path": row["note_path"],
            "fig_index": row["fig_index"],
            "image_url": row.get("image_url", ""),
            "local_path": row.get("local_path", ""),
            "ocr_text": str(analysis.get("ocr_text") or "").strip(),
            "description": str(analysis.get("description") or "").strip(),
            "caption": str(row.get("caption") or ""),
            "token_est": row.get("token_est", 0) or 0,
        }
        try:
            store.upsert_figure(**updated)
        except Exception as exc:  # noqa: BLE001 - DB adapters expose varied errors
            failed += 1
            halted = True
            items.append(
                {**identity, "action": "store_failed", "error": type(exc).__name__}
            )
            break
        applied += 1
        items.append({**identity, "action": "applied"})

    remaining = len(rows) if dry_run else max(0, len(rows) - applied)
    return {
        "backend_policy": "local-only",
        "dry_run": dry_run,
        "note_paths": selected_notes,
        "items": items,
        "summary": {
            "candidates": len(rows),
            "planned": attempted if dry_run else 0,
            "attempted": 0 if dry_run else attempted,
            "applied": applied,
            "failed": failed,
            "manual": manual,
            "remaining_candidates": remaining,
            "halted": halted,
            "paid_model_calls": 0,
        },
    }
