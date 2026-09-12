"""Targeted, bounded enrichment for one registered shared article.

The caller supplies embedding functions explicitly.  This module never discovers or
starts a model service, and it never accepts a server-local source path.  Markdown is
still canonical: every database write is guarded by the article commit lock, a fresh
authorization check, and a content-hash compare against both disk and PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from contextlib import contextmanager
import json
import math
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from .article_attribution import _commit_lock, validate_shared_destination
from .identity import Identity, _current, set_identity
from .note_row import FRONTMATTER_RE, embed_text_for, parse_frontmatter, project_note
from .request_budget import remaining_timeout, request_budget
from .snippets import strip_references
from .vault_paths import resolve_in_vault

EMBEDDING_DIM = 1024
MAX_CHUNKS = 512
MAX_CHUNK_TEXT_BYTES = 64 * 1024
MAX_CHUNK_TEXT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_REGISTRY_BYTES = 10 * 1024 * 1024
REQUEST_BUDGET_SECONDS = 60.0

FIXED_ERROR_CODES = frozenset(
    {
        "article_not_indexed",
        "article_not_registered",
        "article_not_shared",
        "authorization_denied",
        "chunking_unavailable",
        "content_hash_mismatch",
        "database_unavailable",
        "deadline_exceeded",
        "embedding_unavailable",
        "invalid_chunks",
        "invalid_target",
        "invalid_vector",
        "readback_failed",
    }
)


def _complete(*, updated: bool, count: int) -> dict[str, Any]:
    return {"status": "complete", "updated": updated, "count": count}


def _pending(code: str) -> dict[str, Any]:
    if code not in FIXED_ERROR_CODES:
        raise ValueError("unknown enrichment error code")
    return {"status": "pending", "code": code}


def _disabled() -> dict[str, Any]:
    return {"status": "disabled", "updated": False, "count": 0}


def _result(vectors: dict[str, Any], chunks: dict[str, Any]) -> dict[str, Any]:
    return {"vectors": vectors, "chunks": chunks, "figures": _disabled()}


def _same_vector(left: Sequence[float], right: Sequence[float]) -> bool:
    return len(left) == len(right) and all(
        math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)
        for a, b in zip(left, right, strict=True)
    )


def _vector(value: object) -> list[float]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError("invalid vector")
    if len(value) != EMBEDDING_DIM:
        raise ValueError("invalid vector")
    result: list[float] = []
    for item in value:
        if isinstance(item, bool):
            raise ValueError("invalid vector")
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("invalid vector") from None
        if not math.isfinite(number):
            raise ValueError("invalid vector")
        result.append(number)
    return result


def _stored_vector(value: object) -> list[float]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            raise ValueError("invalid vector") from None
    return _vector(value)


def _chunks(value: object) -> list[tuple[str, list[float]]]:
    if value is None:
        raise LookupError("chunking unavailable")
    if isinstance(value, (str, bytes, bytearray)):
        raise ValueError("invalid chunks")
    try:
        iterator: Iterable[object] = iter(value)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("invalid chunks") from None

    result: list[tuple[str, list[float]]] = []
    total_bytes = 0
    for index, item in enumerate(iterator):
        if index >= MAX_CHUNKS:
            raise ValueError("invalid chunks")
        if (
            isinstance(item, (str, bytes, bytearray))
            or not isinstance(item, Sequence)
            or len(item) != 2
        ):
            raise ValueError("invalid chunks")
        text, embedding = item
        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalid chunks")
        text_bytes = len(text.encode("utf-8"))
        total_bytes += text_bytes
        if (
            text_bytes > MAX_CHUNK_TEXT_BYTES
            or total_bytes > MAX_CHUNK_TEXT_TOTAL_BYTES
        ):
            raise ValueError("invalid chunks")
        try:
            vector = _vector(embedding)
        except ValueError:
            raise ValueError("invalid chunks") from None
        result.append((text, vector))
    if not result:
        raise ValueError("invalid chunks")
    return result


def _fresh_identity(
    authorize: Callable[[], Identity], expected_uuid: str | None = None
) -> Identity:
    try:
        identity = authorize()
    except Exception:
        raise PermissionError("authorization denied") from None
    if not isinstance(identity, Identity) or identity.role not in {
        "member",
        "writer",
        "admin",
    }:
        raise PermissionError("authorization denied")
    try:
        actor_uuid = str(UUID(identity.user_uuid or ""))
    except (TypeError, ValueError, AttributeError):
        raise PermissionError("authorization denied") from None
    if expected_uuid is not None and actor_uuid != expected_uuid:
        raise PermissionError("authorization denied")
    return identity


@contextmanager
def _as(identity: Identity):
    token = set_identity(identity)
    try:
        yield
    finally:
        _current.reset(token)


def _registry_target(root: Path, rel: str) -> Path:
    if not isinstance(rel, str):
        raise ValueError("invalid target")
    validate_shared_destination(rel)
    if Path(rel).suffix != ".md":
        raise ValueError("invalid target")
    target = resolve_in_vault(root, rel, must_exist=True)
    if target.relative_to(root).as_posix() != rel:
        raise ValueError("invalid target")

    try:
        registry_path = resolve_in_vault(
            root, ".article-identities.json", must_exist=True
        )
    except (OSError, ValueError):
        raise LookupError("article not registered") from None
    if registry_path != root / ".article-identities.json":
        raise ValueError("invalid target")
    if registry_path.stat().st_size > MAX_REGISTRY_BYTES:
        raise ValueError("invalid target")
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise LookupError("article not registered") from None
    if not isinstance(registry, dict):
        raise LookupError("article not registered")
    records = list(registry.values())
    if not records or not all(
        isinstance(record, dict)
        and isinstance(record.get("path"), str)
        and record.get("index_status") in {"pending", "complete"}
        for record in records
    ):
        raise LookupError("article not registered")
    if not any(record["path"] == rel for record in records):
        raise LookupError("article not registered")
    return target


def _file_snapshot(root: Path, rel: str, expected_hash: str) -> tuple[Path, str, str]:
    target = _registry_target(root, rel)
    note = project_note(root, target)
    if note.path != rel or note.content_hash != expected_hash:
        raise RuntimeError("content hash mismatch")
    raw = target.read_text(encoding="utf-8", errors="strict")
    # project_note can use a truncated read for large notes, but its hash is always
    # over the full file.  A second projection detects a mutation between reads.
    if project_note(root, target).content_hash != note.content_hash:
        raise RuntimeError("content hash mismatch")
    frontmatter = parse_frontmatter(raw)
    embedding_text = (
        f"{frontmatter.get('title', target.stem)} "
        f"{frontmatter.get('tags', '')} {embed_text_for(raw)}"
    ).strip()
    chunk_body = strip_references(FRONTMATTER_RE.sub("", raw).strip())
    return target, embedding_text, chunk_body


def _note_row(store: object, identity: Identity, rel: str, *, for_update: bool = False):
    suffix = " FOR UPDATE" if for_update else ""
    with _as(identity), store._conn(timeout=remaining_timeout(2.0)) as conn:  # type: ignore[attr-defined]
        return conn.execute(
            "SELECT content_hash, owner_id, embedding FROM notes WHERE path = %s" + suffix,
            [rel],
        ).fetchone()


def _preflight(
    store: object,
    root: Path,
    rel: str,
    expected_hash: str,
    identity: Identity,
) -> tuple[Path, str, str, object]:
    target, embedding_text, chunk_body = _file_snapshot(root, rel, expected_hash)
    row = _note_row(store, identity, rel)
    if row is None:
        raise FileNotFoundError("article not indexed")
    if row[1] is not None:
        raise IsADirectoryError("article not shared")
    if row[0] != expected_hash:
        raise RuntimeError("content hash mismatch")
    return target, embedding_text, chunk_body, row[2]


def _existing_chunks(store: object, identity: Identity, rel: str, expected_hash: str):
    with _as(identity), store._conn(timeout=remaining_timeout(2.0)) as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            """
            SELECT chunk_idx, chunk_text, content_hash, embedding, owner_id
            FROM note_chunks WHERE note_path = %s ORDER BY chunk_idx
            """,
            [rel],
        ).fetchall()
    if not rows:
        return None
    if len(rows) > MAX_CHUNKS:
        return None
    expected_indexes = list(range(len(rows)))
    if [row[0] for row in rows] != expected_indexes:
        return None
    validated: list[tuple[str, list[float]]] = []
    try:
        for _, text, content_hash, embedding, owner_id in rows:
            if content_hash != expected_hash or owner_id is not None:
                return None
            validated.append(_chunks([(text, _stored_vector(embedding))])[0])
    except (TypeError, ValueError):
        return None
    return validated


def _write_vector(
    store: object,
    root: Path,
    rel: str,
    expected_hash: str,
    authorize: Callable[[], Identity],
    actor_uuid: str,
    vector: list[float],
) -> dict[str, Any]:
    with _commit_lock(root, remaining_timeout(5.0)):
        _file_snapshot(root, rel, expected_hash)
        identity = _fresh_identity(authorize, actor_uuid)
        with _as(identity), store._conn(timeout=remaining_timeout(2.0)) as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT content_hash, owner_id, embedding FROM notes "
                "WHERE path = %s FOR UPDATE",
                [rel],
            ).fetchone()
            if row is None:
                raise FileNotFoundError("article not indexed")
            if row[1] is not None:
                raise IsADirectoryError("article not shared")
            if row[0] != expected_hash:
                raise RuntimeError("content hash mismatch")
            if row[2] is None:
                conn.execute(
                    "UPDATE notes SET embedding = %s::vector "
                    "WHERE path = %s AND content_hash = %s "
                    "AND owner_id IS NULL AND embedding IS NULL",
                    [str(vector), rel, expected_hash],
                )
            conn.commit()

        readback = _note_row(store, identity, rel)
        if (
            readback is None
            or readback[0] != expected_hash
            or readback[1] is not None
            or readback[2] is None
        ):
            raise LookupError("readback failed")
        stored = _stored_vector(readback[2])
        if not _same_vector(stored, vector):
            raise LookupError("readback failed")
    return _complete(updated=row[2] is None, count=1)


def _write_chunk_rows(
    store: object,
    root: Path,
    rel: str,
    expected_hash: str,
    authorize: Callable[[], Identity],
    actor_uuid: str,
    chunks: list[tuple[str, list[float]]],
) -> dict[str, Any]:
    with _commit_lock(root, remaining_timeout(5.0)):
        _file_snapshot(root, rel, expected_hash)
        identity = _fresh_identity(authorize, actor_uuid)
        with _as(identity), store._conn(timeout=remaining_timeout(2.0)) as conn:  # type: ignore[attr-defined]
            row = conn.execute(
                "SELECT content_hash, owner_id FROM notes WHERE path = %s FOR UPDATE",
                [rel],
            ).fetchone()
            if row is None:
                raise FileNotFoundError("article not indexed")
            if row[1] is not None:
                raise IsADirectoryError("article not shared")
            if row[0] != expected_hash:
                raise RuntimeError("content hash mismatch")
            with conn.cursor() as cursor:
                store._write_chunks(  # type: ignore[attr-defined]
                    cursor, rel, expected_hash, chunks, owner_id=None
                )
            conn.commit()

        readback = _existing_chunks(store, identity, rel, expected_hash)
        if readback is None or len(readback) != len(chunks):
            raise LookupError("readback failed")
        if any(
            actual_text != expected_text
            or not _same_vector(actual_vector, expected_vector)
            for (actual_text, actual_vector), (expected_text, expected_vector)
            in zip(readback, chunks, strict=True)
        ):
            raise LookupError("readback failed")
    return _complete(updated=True, count=len(chunks))


def _error_code(error: BaseException) -> str:
    if isinstance(error, PermissionError):
        return "authorization_denied"
    if isinstance(error, TimeoutError):
        return "deadline_exceeded"
    if isinstance(error, FileNotFoundError):
        return "article_not_indexed"
    if isinstance(error, IsADirectoryError):
        return "article_not_shared"
    if isinstance(error, RuntimeError) and str(error) == "content hash mismatch":
        return "content_hash_mismatch"
    if isinstance(error, LookupError) and str(error) == "article not registered":
        return "article_not_registered"
    if isinstance(error, LookupError) and str(error) == "readback failed":
        return "readback_failed"
    if isinstance(error, (ValueError, OSError, UnicodeError)):
        return "invalid_target"
    return "database_unavailable"


def enrich_shared_article(
    store: object,
    vault: Path,
    rel: str,
    *,
    expected_hash: str,
    authorize: Callable[[], Identity],
    embed: Callable[[str], object],
    chunk_embed: Callable[[str], object],
    figures: object = None,
) -> dict[str, Any]:
    """Enrich exactly one canonical shared article with vector and chunk rows.

    The result contains only fixed stage states and error codes.  Provider
    exceptions, paths, article text, and database details are never returned.
    ``figures`` is reserved for a later, separately controlled implementation;
    this version never calls it and always reports that stage as disabled.
    """

    del figures
    if not isinstance(expected_hash, str) or not re.fullmatch(
        r"[0-9a-f]{32}", expected_hash
    ):
        invalid = _pending("content_hash_mismatch")
        return _result(invalid, invalid.copy())
    unavailable = _pending("authorization_denied")
    try:
        identity = _fresh_identity(authorize)
        actor_uuid = str(UUID(identity.user_uuid or ""))
    except PermissionError:
        return _result(unavailable, unavailable.copy())

    try:
        with request_budget(REQUEST_BUDGET_SECONDS):
            try:
                with _commit_lock(Path(vault).resolve(), remaining_timeout(5.0)):
                    root = Path(vault).resolve()
                    _, embedding_text, _, existing_vector = _preflight(
                        store, root, rel, expected_hash, identity
                    )
            except Exception as error:
                code = _error_code(error)
                return _result(_pending(code), _pending(code))

            if existing_vector is not None:
                try:
                    _stored_vector(existing_vector)
                except ValueError:
                    vector_status = _pending("invalid_vector")
                else:
                    vector_status = _complete(updated=False, count=1)
            else:
                try:
                    _fresh_identity(authorize, actor_uuid)
                    remaining_timeout(REQUEST_BUDGET_SECONDS)
                    provided_vector = embed(embedding_text)
                    remaining_timeout(REQUEST_BUDGET_SECONDS)
                except PermissionError:
                    vector_status = _pending("authorization_denied")
                except TimeoutError:
                    vector_status = _pending("deadline_exceeded")
                except Exception:
                    vector_status = _pending("embedding_unavailable")
                else:
                    if provided_vector is None:
                        vector_status = _pending("embedding_unavailable")
                    else:
                        try:
                            vector = _vector(provided_vector)
                        except ValueError:
                            vector_status = _pending("invalid_vector")
                        else:
                            try:
                                vector_status = _write_vector(
                                    store,
                                    root,
                                    rel,
                                    expected_hash,
                                    authorize,
                                    actor_uuid,
                                    vector,
                                )
                            except Exception as error:
                                vector_status = _pending(_error_code(error))

            try:
                chunk_identity = _fresh_identity(authorize, actor_uuid)
                with _commit_lock(root, remaining_timeout(5.0)):
                    _, _, current_chunk_body, _ = _preflight(
                        store, root, rel, expected_hash, chunk_identity
                    )
                    existing_chunks = _existing_chunks(
                        store, chunk_identity, rel, expected_hash
                    )
            except Exception as error:
                return _result(vector_status, _pending(_error_code(error)))

            if existing_chunks is not None:
                chunk_status = _complete(updated=False, count=len(existing_chunks))
            else:
                try:
                    _fresh_identity(authorize, actor_uuid)
                    remaining_timeout(REQUEST_BUDGET_SECONDS)
                    provided_chunks = chunk_embed(current_chunk_body)
                    remaining_timeout(REQUEST_BUDGET_SECONDS)
                except PermissionError:
                    chunk_status = _pending("authorization_denied")
                except TimeoutError:
                    chunk_status = _pending("deadline_exceeded")
                except Exception:
                    chunk_status = _pending("chunking_unavailable")
                else:
                    try:
                        chunks = _chunks(provided_chunks)
                    except LookupError:
                        chunk_status = _pending("chunking_unavailable")
                    except ValueError:
                        chunk_status = _pending("invalid_chunks")
                    else:
                        try:
                            chunk_status = _write_chunk_rows(
                                store,
                                root,
                                rel,
                                expected_hash,
                                authorize,
                                actor_uuid,
                                chunks,
                            )
                        except Exception as error:
                            chunk_status = _pending(_error_code(error))
            return _result(vector_status, chunk_status)
    except TimeoutError:
        return _result(_pending("deadline_exceeded"), _pending("deadline_exceeded"))


__all__ = [
    "EMBEDDING_DIM",
    "FIXED_ERROR_CODES",
    "MAX_CHUNKS",
    "MAX_CHUNK_TEXT_BYTES",
    "REQUEST_BUDGET_SECONDS",
    "enrich_shared_article",
]
