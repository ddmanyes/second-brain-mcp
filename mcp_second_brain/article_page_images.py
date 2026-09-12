"""Publish bounded document-page images for one verified shared article.

Pages retain visual source material without claiming semantic figure extraction,
OCR, or model-generated descriptions. Existing unrelated figure rows are never
replaced. Files publish before a short SQL transaction; interrupted publication
is safe to retry with the same verified page bytes.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from pathlib import Path
from uuid import UUID

from PIL import Image

from .article_enrichment import _registry_target
from .article_attribution import _commit_lock, validate_shared_destination
from .identity import Identity, _current, set_identity
from .note_row import project_note
from .vault_paths import resolve_in_vault


def publish_article_pages(
    store, vault, rel, staging_root, pages, *, expected_hash, authorize
):
    root = Path(vault).resolve()
    staging = Path(staging_root).resolve()
    validate_shared_destination(rel)
    if not isinstance(pages, list) or not 1 <= len(pages) <= 200:
        raise ValueError("invalid page image count")
    prepared = []
    total = 0
    folder = "figures/managed-" + hashlib.sha256(rel.encode()).hexdigest()[:20]
    for index, page in enumerate(pages):
        if page.get("index") != index + 1 or page.get("mime") != "image/png":
            raise ValueError("invalid page image sequence")
        source = staging / page["relative_staging_path"]
        if source.is_symlink() or source.resolve().is_relative_to(staging) is False:
            raise ValueError("invalid page image location")
        if source.stat().st_size > 2 * 1024**2:
            raise ValueError("page image size exceeded")
        data = source.read_bytes()
        total += len(data)
        digest = hashlib.sha256(data).hexdigest()
        if digest != page.get("sha256") or total > 100 * 1024**2:
            raise ValueError("page artifact integrity failed")
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "PNG" or max(image.size) > 1024 or min(image.size) <= 0:
                raise ValueError("invalid page image dimensions")
            image.verify()
        destination = resolve_in_vault(
            root, f"{folder}/fig-{index:03d}-{digest[:12]}.png", must_exist=False
        )
        prepared.append((destination, data, digest))

    def identity(expected_uuid=None):
        try:
            actor = authorize()
            actor_uuid = str(UUID(actor.user_uuid or ""))
        except Exception:
            raise PermissionError("article image authorization denied") from None
        if not isinstance(actor, Identity) or not actor.can_write():
            raise PermissionError("article image authorization denied")
        if expected_uuid is not None and actor_uuid != expected_uuid:
            raise PermissionError("article image authorization denied")
        return actor, actor_uuid

    expected_existing = [
        (index, str(path), None)
        for index, (path, _data, _digest) in enumerate(prepared)
    ]

    def validate_existing(rows):
        if any(row[2] is not None for row in rows):
            raise PermissionError("article image authorization denied")
        if rows and rows != expected_existing:
            raise ValueError("existing article figures require reviewed maintenance")

    with _commit_lock(root, 1):
        actor, actor_uuid = identity()
        token = set_identity(actor)
        try:
            document = _registry_target(root, rel)
            note = project_note(root, document)
            if note.content_hash != expected_hash:
                raise ValueError("article content changed")
            with store._conn(timeout=0.5) as conn:
                existing = conn.execute(
                    "SELECT fig_index, local_path, owner_id FROM figures "
                    "WHERE note_path=%s ORDER BY fig_index",
                    [rel],
                ).fetchall()
            validate_existing(existing)
            for path, data, _digest in prepared:
                identity(actor_uuid)
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=".page-", dir=path.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    directory = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            identity(actor_uuid)
            if project_note(root, document).content_hash != expected_hash:
                raise ValueError("article content changed")
            with store._conn(timeout=0.5) as conn:
                row = conn.execute(
                    "SELECT content_hash, owner_id FROM notes WHERE path=%s FOR UPDATE",
                    [rel],
                ).fetchone()
                if row != (expected_hash, None):
                    raise ValueError("article index changed")
                existing = conn.execute(
                    "SELECT fig_index, local_path, owner_id FROM figures "
                    "WHERE note_path=%s ORDER BY fig_index FOR UPDATE",
                    [rel],
                ).fetchall()
                validate_existing(existing)
                for index, (path, _data, _digest) in enumerate(prepared):
                    conn.execute(
                        """INSERT INTO figures (note_path,fig_index,image_url,local_path,ocr_text,description,token_est,caption,owner_id)
                        VALUES (%s,%s,'',%s,'',%s,0,%s,NULL)
                        ON CONFLICT (note_path,fig_index) DO UPDATE SET local_path=EXCLUDED.local_path,
                        image_url=EXCLUDED.image_url,ocr_text=EXCLUDED.ocr_text,
                        description=EXCLUDED.description,token_est=EXCLUDED.token_est,
                        caption=EXCLUDED.caption,owner_id=NULL""",
                        [
                            rel,
                            index,
                            str(path),
                            f"Document page {index + 1}",
                            f"Page {index + 1}",
                        ],
                    )
                stored = conn.execute(
                    "SELECT fig_index,image_url,local_path,ocr_text,description,"
                    "caption,coalesce(token_est,0),owner_id FROM figures "
                    "WHERE note_path=%s ORDER BY fig_index",
                    [rel],
                ).fetchall()
                expected_metadata = [
                    (
                        index,
                        "",
                        str(path),
                        "",
                        f"Document page {index + 1}",
                        f"Page {index + 1}",
                        0,
                        None,
                    )
                    for index, (path, _data, _digest) in enumerate(prepared)
                ]
                if stored != expected_metadata:
                    raise RuntimeError("article page readback failed")
                conn.commit()
            rows = store.get_figures_for_note(rel)
            expected_readback = [
                {
                    "note_path": rel,
                    "fig_index": index,
                    "image_url": "",
                    "local_path": str(path),
                    "ocr_text": "",
                    "description": f"Document page {index + 1}",
                    "caption": f"Page {index + 1}",
                    "token_est": 0,
                }
                for index, (path, _data, _digest) in enumerate(prepared)
            ]
            if rows != expected_readback:
                raise RuntimeError("article page readback failed")
            for path, _, digest in prepared:
                if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                    raise RuntimeError("article page readback failed")
            return {
                "status": "complete",
                "count": len(prepared),
                "kind": "document_pages",
            }
        finally:
            _current.reset(token)
