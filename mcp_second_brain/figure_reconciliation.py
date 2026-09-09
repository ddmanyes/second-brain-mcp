"""Deterministic, zero-Vision reconciliation for figure files and projections.

The research Markdown remains canonical and read-only here.  This module only
materializes verified image bytes below ``vault/figures`` and upserts the
rebuildable figure projection through the selected :class:`VaultStore`.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from PIL import Image, UnidentifiedImageError

from .figures import (
    IMG_RE,
    _estimate_image_tokens,
    _figure_slug,
    _is_content_image,
    _is_ssrf_safe,
    _parse_source_url,
    _resolve_url,
)

_MAX_IMAGE_BYTES = 20 * 1024 * 1024
_MAX_REDIRECTS = 5
_FIGURE_NAME_RE = re.compile(r"^fig-(\d+)\.(png|jpe?g|webp|gif)$", re.IGNORECASE)
_CONTENT_TYPE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_note(vault: Path, note_path: str) -> Path:
    candidate = (vault / note_path).resolve()
    if not candidate.is_relative_to(vault):
        raise ValueError(f"note path is outside vault: {note_path}")
    return candidate


def _is_safe_local_file(path: Path, figures_root: Path) -> bool:
    """Reject symlinks and anything whose resolved target escapes figures/."""
    try:
        if path.is_symlink() or not path.is_file():
            return False
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(figures_root):
            return False
        current = path.parent
        while current != figures_root:
            if current.is_symlink():
                return False
            if current == current.parent:
                return False
            current = current.parent
        return True
    except (OSError, RuntimeError):
        return False


def _remote_images(md_text: str) -> list[dict]:
    source_url = _parse_source_url(md_text) or ""
    images: list[dict] = []
    for alt, image_ref in IMG_RE.findall(md_text):
        if not _is_content_image(alt, image_ref):
            continue
        absolute = _resolve_url(image_ref.strip(), source_url)
        if not absolute or urlparse(absolute).scheme not in {"http", "https"}:
            continue
        images.append({"url": absolute, "alt": alt.strip()})
    return images


def _local_candidates(folder: Path) -> dict[int, list[Path]]:
    candidates: dict[int, list[Path]] = {}
    if not folder.exists():
        return candidates
    for path in sorted(folder.glob("fig-*")):
        match = _FIGURE_NAME_RE.fullmatch(path.name)
        if match:
            candidates.setdefault(int(match.group(1)), []).append(path)
    return candidates


def _existing_path(row: dict, figures_root: Path) -> Path | None:
    raw = str(row.get("local_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = figures_root.parent / path
    return path


def _text_proxy(row: dict | None, remote: dict | None) -> tuple[str, str, str]:
    row = row or {}
    alt = (remote or {}).get("alt", "").strip()
    return (
        str(row.get("ocr_text") or ""),
        str(row.get("description") or ""),
        str(row.get("caption") or alt),
    )


def _plan_note(note_path: str, vault: Path, store) -> dict:
    md_file = _safe_note(vault, note_path)
    if not md_file.is_file():
        return {
            "note_path": note_path,
            "actions": [
                {
                    "fig_index": None,
                    "action": "manual_queue",
                    "reason": "note_missing",
                    "checksum": None,
                }
            ],
        }

    md_text = md_file.read_text(encoding="utf-8")
    remote_images = _remote_images(md_text)
    figures_root = (vault / "figures").resolve()
    folder = figures_root / _figure_slug(note_path)
    candidates = _local_candidates(folder)
    rows = {int(row["fig_index"]): row for row in store.get_figures_for_note(note_path)}
    indices = sorted(set(rows) | set(candidates) | set(range(len(remote_images))))
    actions: list[dict] = []

    for fig_index in indices:
        row = rows.get(fig_index)
        remote = remote_images[fig_index] if fig_index < len(remote_images) else None
        local = candidates.get(fig_index, [])
        unsafe = [p for p in local if not _is_safe_local_file(p, figures_root)]
        safe = [p for p in local if p not in unsafe]

        base = {"fig_index": fig_index, "checksum": None}
        if unsafe:
            actions.append(
                {
                    **base,
                    "action": "manual_queue",
                    "reason": "unsafe_local_candidate",
                    "candidates": [str(p) for p in local],
                }
            )
            continue
        if len(safe) > 1:
            actions.append(
                {
                    **base,
                    "action": "manual_queue",
                    "reason": "multiple_local_candidates",
                    "candidates": [str(p.resolve()) for p in safe],
                }
            )
            continue

        current = _existing_path(row, figures_root) if row else None
        if row and current and _is_safe_local_file(current, figures_root):
            actions.append(
                {
                    **base,
                    "action": "no_action",
                    "reason": "already_reconciled",
                    "local_path": str(current.resolve()),
                    "checksum": _sha256(current.resolve()),
                }
            )
            continue

        if row and current and current.exists():
            actions.append(
                {
                    **base,
                    "action": "manual_queue",
                    "reason": "unsafe_existing_path",
                    "local_path": str(current),
                }
            )
            continue

        if safe:
            path = safe[0].resolve()
            action = "repair_local_path" if row else "index_local_file"
            actions.append(
                {
                    **base,
                    "action": action,
                    "reason": (
                        "unique_same_stem_candidate"
                        if row
                        else "unique_canonical_local_file"
                    ),
                    "local_path": str(path),
                    "image_url": (remote or {}).get("url") or f"file://{path}",
                    "checksum": _sha256(path),
                    "text_proxy": dict(
                        zip(
                            ("ocr_text", "description", "caption"),
                            _text_proxy(row, remote),
                        )
                    ),
                    "existing_row": row,
                }
            )
            continue

        source_url = ""
        if row and urlparse(str(row.get("image_url") or "")).scheme in {
            "http",
            "https",
        }:
            source_url = str(row["image_url"])
        elif remote:
            source_url = remote["url"]
        ocr_text, description, caption = _text_proxy(row, remote)
        if source_url and (ocr_text or description or caption):
            actions.append(
                {
                    **base,
                    "action": "materialize_remote_image",
                    "reason": "verified_remote_source_and_text_proxy",
                    "image_url": source_url,
                    "destination_stem": str(folder / f"fig-{fig_index:02d}"),
                    "text_proxy": {
                        "ocr_text": ocr_text,
                        "description": description,
                        "caption": caption,
                    },
                    "existing_row": row,
                }
            )
        elif source_url:
            actions.append(
                {
                    **base,
                    "action": "manual_queue",
                    "reason": "empty_alt_text",
                    "image_url": source_url,
                }
            )
        elif row:
            actions.append(
                {
                    **base,
                    "action": "manual_queue",
                    "reason": "missing_local_file_without_remote_source",
                    "local_path": str(current) if current else "",
                }
            )

    return {"note_path": note_path, "actions": actions}


def _validate_image(path: Path) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
        return True
    except (OSError, SyntaxError, UnidentifiedImageError):
        return False


def _materialize_remote_image(url: str, destination_stem: Path) -> dict:
    """Fetch one image with bounded redirects and atomically place verified bytes."""
    current_url = url
    temp_path: Path | None = None
    try:
        for redirect_count in range(_MAX_REDIRECTS + 1):
            if not _is_ssrf_safe(current_url):
                return {"ok": False, "reason": "unsafe_remote_url"}
            with requests.get(
                current_url,
                timeout=20,
                headers={"User-Agent": "Mozilla/5.0"},
                stream=True,
                allow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location", "")
                    if not location:
                        return {"ok": False, "reason": "redirect_without_location"}
                    if redirect_count == _MAX_REDIRECTS:
                        return {"ok": False, "reason": "too_many_redirects"}
                    current_url = urljoin(current_url, location)
                    continue
                if response.status_code != 200:
                    return {
                        "ok": False,
                        "reason": "download_http_error",
                        "status_code": response.status_code,
                    }
                content_type = (
                    response.headers.get("Content-Type", "").split(";", 1)[0].lower()
                )
                extension = _CONTENT_TYPE_EXTENSIONS.get(content_type)
                if not extension:
                    return {"ok": False, "reason": "download_not_image"}
                try:
                    declared_size = int(response.headers.get("Content-Length", "0"))
                except ValueError:
                    declared_size = 0
                if declared_size > _MAX_IMAGE_BYTES:
                    return {"ok": False, "reason": "download_too_large"}

                destination_stem.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    prefix=f".{destination_stem.name}-",
                    suffix=extension,
                    dir=destination_stem.parent,
                    delete=False,
                ) as handle:
                    temp_path = Path(handle.name)
                    total = 0
                    for chunk in response.iter_content(8192):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_IMAGE_BYTES:
                            return {"ok": False, "reason": "download_too_large"}
                        handle.write(chunk)
                if not temp_path.stat().st_size:
                    return {"ok": False, "reason": "download_empty"}
                if not _validate_image(temp_path):
                    return {"ok": False, "reason": "download_invalid_image"}

                destination = destination_stem.with_suffix(extension)
                checksum = _sha256(temp_path)
                if destination.exists():
                    if not _is_safe_local_file(
                        destination, destination_stem.parents[1]
                    ):
                        return {"ok": False, "reason": "destination_conflict"}
                    if _sha256(destination) != checksum:
                        return {"ok": False, "reason": "destination_conflict"}
                    temp_path.unlink(missing_ok=True)
                    temp_path = None
                else:
                    os.replace(temp_path, destination)
                    temp_path = None
                return {
                    "ok": True,
                    "local_path": str(destination.resolve()),
                    "checksum": checksum,
                    "content_type": content_type,
                    "final_url": current_url,
                }
        return {"ok": False, "reason": "too_many_redirects"}
    except requests.RequestException:
        return {"ok": False, "reason": "download_request_failed"}
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _upsert_and_verify(action: dict, note_path: str, store) -> dict:
    row = action.get("existing_row") or {}
    text = action.get("text_proxy") or {}
    store.upsert_figure(
        note_path=note_path,
        fig_index=action["fig_index"],
        image_url=action.get("image_url") or str(row.get("image_url") or ""),
        local_path=action["local_path"],
        ocr_text=text.get("ocr_text", str(row.get("ocr_text") or "")),
        description=text.get("description", str(row.get("description") or "")),
        caption=text.get("caption", str(row.get("caption") or "")),
        token_est=int(
            row.get("token_est") or _estimate_image_tokens(Path(action["local_path"]))
        ),
    )
    verified = store.get_figure(note_path, action["fig_index"])
    if not verified or str(verified.get("local_path")) != action["local_path"]:
        raise RuntimeError("figure projection verification failed")
    if _sha256(Path(action["local_path"])) != action["checksum"]:
        raise RuntimeError("figure checksum verification failed")
    return {**action, "status": "applied"}


def reconcile_figures(
    note_paths: list[str],
    vault: Path,
    store,
    *,
    dry_run: bool = True,
    limit: int = 20,
) -> dict:
    """Plan or apply deterministic reconciliation for at most twenty notes."""
    if not 1 <= limit <= 20:
        raise ValueError("limit must be between 1 and 20")
    if len(note_paths) > 20:
        raise ValueError("reconcile_figures accepts at most 20 note paths")
    vault = vault.expanduser().resolve()
    selected = list(dict.fromkeys(note_paths))[:limit]
    notes = [_plan_note(note_path, vault, store) for note_path in selected]

    summary = {
        "planned": 0,
        "applied": 0,
        "manual": 0,
        "no_action": 0,
        "failed": 0,
        "halted": False,
    }
    halted = False
    for note in notes:
        updated: list[dict] = []
        for action in note["actions"]:
            kind = action["action"]
            if kind == "manual_queue":
                summary["manual"] += 1
                updated.append(action)
                continue
            if kind == "no_action":
                summary["no_action"] += 1
                updated.append(action)
                continue
            summary["planned"] += 1
            if dry_run:
                updated.append(action)
                continue
            if kind == "materialize_remote_image":
                fetched = _materialize_remote_image(
                    action["image_url"], Path(action["destination_stem"])
                )
                if not fetched["ok"]:
                    summary["manual"] += 1
                    updated.append(
                        {
                            **action,
                            "action": "manual_queue",
                            "reason": fetched["reason"],
                        }
                    )
                    continue
                action = {
                    **action,
                    "local_path": fetched["local_path"],
                    "checksum": fetched["checksum"],
                    "image_url": fetched["final_url"],
                }
            try:
                updated.append(_upsert_and_verify(action, note["note_path"], store))
                summary["applied"] += 1
            except Exception as exc:  # noqa: BLE001 — store boundary may raise backend-specific errors
                summary["failed"] += 1
                summary["halted"] = True
                halted = True
                updated.append(
                    {
                        **action,
                        "status": "failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                break
        note["actions"] = updated
        if halted:
            break

    return {
        "dry_run": dry_run,
        "limit": limit,
        "notes": notes,
        "summary": summary,
    }
