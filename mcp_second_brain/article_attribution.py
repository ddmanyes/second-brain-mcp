"""Shared article commits from already validated intake, separate from note ownership.

This module does not fetch or convert sources. A future worker must validate its
source/redirects and metadata before calling it. Registry entries cover commits
made through this helper; historical vault-wide dedup needs a separate backfill.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from . import frontmatter
from .article_metadata import normalise_doi
from .identity import Identity
from .note_row import FRONTMATTER_RE, normalise_keyword_list, parse_frontmatter
from .vault_paths import resolve_in_vault


def _remove_fields(content: str, fields: set[str]) -> str:
    match = FRONTMATTER_RE.match(content)
    if not match:
        raise ValueError("article requires flat frontmatter")
    lines = [line for line in match.group(1).splitlines()
             if line.partition(":")[0].strip() not in fields]
    return "---\n" + "\n".join(lines) + "\n---\n\n" + content[match.end():]


def attribute_article(content: str, identity: Identity, *, created: bool) -> str:
    """Trusted request UUID only; existing metadata is trusted server history."""
    actor = str(UUID(identity.user_uuid or ""))
    metadata = parse_frontmatter(content)
    if metadata.get("owner_id") not in (None, "", "null"):
        raise ValueError("private ownership cannot be converted to shared attribution")
    # Fresh conversion content must never forge historical contributors/uploader.
    raw = "[]" if created else metadata.get("contributor_ids", "[]")
    contributors = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(contributors, list):
        raise ValueError("invalid article contributors")
    contributors = list(dict.fromkeys(str(UUID(value)) for value in contributors))
    if actor not in contributors:
        contributors.append(actor)
    tags = json.loads(normalise_keyword_list(metadata.get("tags", "")) or "[]")
    tags = [tag for tag in tags if isinstance(tag, str) and not tag.startswith("uploaded-by-")]
    tags.extend(f"uploaded-by-{value}" for value in contributors)
    content = _remove_fields(content, {"owner_id"})
    fields = {
        "contributor_ids": json.dumps(contributors),
        "tags": json.dumps(list(dict.fromkeys(tags)), ensure_ascii=False),
    }
    if created:
        fields["uploaded_by"] = json.dumps(actor)
    return frontmatter.set_fields(content, fields)


def canonical_source(source: str) -> str:
    """Normalize URLs without dropping potentially meaningful query parameters."""
    try:
        parsed = urlsplit(source)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
        if parsed.username is not None or parsed.password is not None:
            raise ValueError
        port = parsed.port
    except ValueError:
        raise ValueError("article source must be an HTTP(S) URL without credentials") from None
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and (parsed.scheme.lower(), port) not in {("https", 443), ("http", 80)}:
        host += f":{port}"
    return urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))


def article_identity(source: str, body: str, *, verified_metadata: dict | None = None) -> str:
    """Caller must supply resolver-verified metadata, never raw member claims."""
    metadata = verified_metadata or {}
    if metadata.get("doi"):
        doi = normalise_doi(metadata["doi"])
        if not re.fullmatch(r"10\.\d{4,9}/\S+", doi):
            raise ValueError("invalid verified DOI")
        return "doi:" + doi
    if metadata.get("pmid"):
        pmid = str(metadata["pmid"]).strip()
        if not re.fullmatch(r"[1-9]\d*", pmid):
            raise ValueError("invalid verified PMID")
        return "pmid:" + pmid
    if metadata.get("sha256"):
        digest = str(metadata["sha256"]).lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid verified source hash")
        return "sha256:" + digest
    if metadata.get("canonical_url") or source:
        return "url:" + canonical_source(str(metadata.get("canonical_url") or source))
    return "sha256:" + hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_shared_destination(rel: str) -> None:
    parts = PurePosixPath(rel).parts
    if ("\\" in rel or ".." in parts or PurePosixPath(rel).is_absolute()
            or not (parts[:1] == ("30-resources",) or parts[:2] == ("20-areas", "research"))):
        raise ValueError("member articles must use shared 30-resources or 20-areas/research")


@contextmanager
def _commit_lock(vault: Path, timeout: float):
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(vault / ".article-commit.lock", flags, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("shared article commit busy; retry later") from None
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".article-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
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


@dataclass(frozen=True)
class ArticleCommit:
    path: str
    created: bool
    index_status: str  # complete or pending; pending never claims completion


def commit_shared_article(
    vault: Path, rel: str, content: str, identity: Identity, *, source: str,
    index: Callable[[Path], bool], verified_metadata: dict | None = None,
    lock_timeout: float = 5.0, authorize: Callable[[], bool] | None = None,
) -> ArticleCommit:
    """Serialize dedup/contributions and atomically persist before indexing.

    Index callback must be a bounded metadata-only write returning exactly True
    on success, without network/model enrichment. Pending is retryable: the
    durable reservation points later attempts back to the same canonical file.
    """
    if identity.role not in {"member", "writer", "admin"}:
        raise PermissionError("article contribution requires write permission")
    UUID(identity.user_uuid or "")
    validate_shared_destination(rel)
    if Path(rel).suffix != ".md":
        raise ValueError("article target must be a Markdown file")
    root = Path(vault).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = resolve_in_vault(root, rel, must_exist=False)
    validate_shared_destination(target.relative_to(root).as_posix())
    body = FRONTMATTER_RE.sub("", content)
    key = article_identity(source, body, verified_metadata=verified_metadata)
    aliases = {key}
    if source:
        aliases.add(article_identity(source, body))
    for field in ("doi", "pmid", "canonical_url", "sha256"):
        if (verified_metadata or {}).get(field):
            aliases.add(article_identity("", body, verified_metadata={field: verified_metadata[field]}))
    registry_path = resolve_in_vault(root, ".article-identities.json", must_exist=False)
    if registry_path != root / ".article-identities.json":
        raise ValueError("article registry must not be a symlink")
    with _commit_lock(root, lock_timeout):
        if authorize is not None and authorize() is not True:
            raise PermissionError("article contribution authorization expired")
        registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
        if not isinstance(registry, dict) or not all(
            isinstance(k, str) and isinstance(v, dict)
            and isinstance(v.get("path"), str)
            and v.get("index_status") in {"pending", "complete"}
            for k, v in registry.items()
        ):
            raise ValueError("invalid article identity registry")
        matches = {registry[alias]["path"] for alias in aliases if alias in registry}
        if len(matches) > 1:
            raise ValueError("article identifiers conflict with multiple existing records")
        canonical_rel = next(iter(matches), rel)
        if not matches and any(record["path"] == canonical_rel for record in registry.values()):
            raise FileExistsError("article filename is reserved for a different source")
        validate_shared_destination(canonical_rel)
        target = resolve_in_vault(root, canonical_rel, must_exist=False)
        validate_shared_destination(target.relative_to(root).as_posix())
        created = not target.exists()
        canonical_key = key
        if not created:
            existing = target.read_text(encoding="utf-8")
            fm = parse_frontmatter(existing)
            existing_key = fm.get("article_identity") or article_identity(
                fm.get("source", ""), FRONTMATTER_RE.sub("", existing)
            )
            if existing_key not in aliases and registry.get(existing_key, {}).get("path") != canonical_rel:
                raise FileExistsError("article filename collision with a different source")
            canonical_key = existing_key
            content = existing  # never replace canonical article body with a new upload
        content = attribute_article(content, identity, created=created)
        content = frontmatter.set_fields(content, {"article_identity": json.dumps(canonical_key)})
        # Reserve first: a crash before file publication can resume at the same
        # path even when the retry proposes another filename.
        aliases.update(alias for alias, record in registry.items() if record["path"] == canonical_rel)
        for alias in aliases:
            registry[alias] = {"path": canonical_rel, "index_status": "pending"}
        _atomic_write(registry_path, json.dumps(registry, ensure_ascii=False, sort_keys=True))
        _atomic_write(target, content)
        try:
            complete = (authorize is None or authorize() is True) and index(target) is True
        except Exception:
            complete = False
        if complete:
            for alias in aliases:
                registry[alias]["index_status"] = "complete"
            _atomic_write(registry_path, json.dumps(registry, ensure_ascii=False, sort_keys=True))
        return ArticleCommit(canonical_rel, created, "complete" if complete else "pending")
