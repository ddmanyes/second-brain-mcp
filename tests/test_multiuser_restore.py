"""Disposable multiuser backup/restore rehearsal with database-enforced RLS."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath
from uuid import uuid4

import psycopg
import pytest

from mcp_second_brain.note_row import parse_frontmatter
from mcp_second_brain.store.migrate_multiuser import apply_multiuser_schema

pytestmark = pytest.mark.usefixtures("_reset_store_singleton")


def _owned_docker(pg, *arguments: str, input_bytes: bytes | None = None):
    pg._verify_container()
    return subprocess.run(
        ["docker", "exec", "-i", pg.name, *arguments],
        input=input_bytes,
        capture_output=True,
        check=False,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_backup(source_vault: Path, source_workspace: Path, backup: Path, dump: bytes):
    files = backup / "files"
    shutil.copytree(source_vault, files / "vault")
    shutil.copytree(source_workspace, files / "workspace")
    (backup / "database.dump").write_bytes(dump)
    manifest = {
        path.relative_to(backup).as_posix(): _sha256(path)
        for path in sorted(backup.rglob("*"))
        if path.is_file()
    }
    (backup / "sha256-manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def _verify_backup(backup: Path) -> dict[str, str]:
    try:
        manifest = json.loads(
            (backup / "sha256-manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        raise ValueError("backup manifest is invalid") from None
    if not isinstance(manifest, dict) or not manifest:
        raise ValueError("backup manifest is invalid")
    actual = {
        path.relative_to(backup).as_posix()
        for path in backup.rglob("*")
        if path.is_file() and path.name != "sha256-manifest.json"
    }
    if actual != set(manifest):
        raise ValueError("backup contents do not match manifest")
    for relative, expected in manifest.items():
        parts = PurePosixPath(relative)
        if (
            not isinstance(expected, str)
            or parts.is_absolute()
            or ".." in parts.parts
            or len(expected) != 64
        ):
            raise ValueError("backup manifest is invalid")
        path = backup.joinpath(*parts.parts)
        if path.is_symlink() or _sha256(path) != expected:
            raise ValueError("backup checksum mismatch")
    return manifest


def _restore_files(backup: Path, destination: Path) -> None:
    _verify_backup(backup)
    shutil.copytree(backup / "files" / "vault", destination / "vault")
    shutil.copytree(backup / "files" / "workspace", destination / "workspace")


def _visible_paths(pg, actor: str, table: str, path_column: str) -> set[str]:
    if table not in {"notes", "note_chunks", "figures"}:
        raise ValueError("unsupported RLS table")
    with psycopg.connect(pg.dsn(role="sb_app")) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('sb.actor_id', %s, false), "
                "set_config('sb.actor_is_admin', 'off', false)",
                [actor],
            )
            cursor.execute(f"SELECT {path_column} FROM {table}")
            return {row[0] for row in cursor.fetchall()}


def test_multiuser_backup_restore_preserves_rls_and_durable_records(
    tmp_path, clean_multiuser_postgres, record_property
):
    pg = clean_multiuser_postgres
    with psycopg.connect(pg.dsn(role="postgres")) as connection:
        apply_multiuser_schema(connection)
    pg.set_sb_app_password()

    actor_a, actor_b = str(uuid4()), str(uuid4())
    private_a = f"90-personal/{actor_a}/20-areas/research/a-private.md"
    private_b = f"90-personal/{actor_b}/20-areas/research/b-private.md"
    shared = "20-areas/research/shared-article.md"
    rows = [
        (private_a, "A private", actor_a),
        (private_b, "B private", actor_b),
        (shared, "Shared article", None),
    ]
    with psycopg.connect(pg.dsn(role="postgres")) as connection:
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO notes "
                "(path, title, note_type, status, body_snippet, content_hash, owner_id) "
                "VALUES (%s, %s, 'research', 'active', %s, %s, %s)",
                [
                    (path, title, f"body for {title}", f"hash-{index}", owner)
                    for index, (path, title, owner) in enumerate(rows)
                ],
            )
            cursor.executemany(
                "INSERT INTO note_chunks "
                "(note_path, chunk_idx, chunk_text, content_hash, owner_id) "
                "VALUES (%s, 0, %s, %s, %s)",
                [
                    (path, f"chunk for {title}", f"hash-{index}", owner)
                    for index, (path, title, owner) in enumerate(rows)
                ],
            )
            cursor.executemany(
                "INSERT INTO figures "
                "(note_path, fig_index, local_path, ocr_text, description, owner_id) "
                "VALUES (%s, 0, %s, %s, 'synthetic figure', %s)",
                [
                    (path, f"figures/{index}/fig-00.png", f"figure for {title}", owner)
                    for index, (path, title, owner) in enumerate(rows)
                ],
            )
        connection.commit()

    source_vault = tmp_path / "source-vault"
    source_workspace = tmp_path / "source-workspace"
    contributors = [actor_a, actor_b]
    article = source_vault / shared
    article.parent.mkdir(parents=True)
    article.write_text(
        "---\n"
        'title: "Shared article"\n'
        "type: research\n"
        f"uploaded_by: {actor_a}\n"
        f"contributor_ids: '{json.dumps(contributors)}'\n"
        'article_identity: "doi:10.1234/restore"\n'
        "---\n\nSynthetic shared article body.\n",
        encoding="utf-8",
    )
    for path, title, _owner in rows[:2]:
        note = source_vault / path
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"---\ntitle: {title}\n---\n\nPrivate body.\n", encoding="utf-8")
    figure = source_vault / "figures" / "shared" / "fig-00.png"
    figure.parent.mkdir(parents=True)
    figure.write_bytes(b"synthetic-figure-bytes")
    registry = {
        "doi:10.1234/restore": {"path": shared, "index_status": "complete"},
        "url:https://example.org/restore": {
            "path": shared,
            "index_status": "complete",
        },
    }
    (source_vault / ".article-identities.json").write_text(
        json.dumps(registry, sort_keys=True), encoding="utf-8"
    )
    queued_job = {
        "id": "20260912-restore-synthetic",
        "kind": "ingest",
        "managed_protocol": 1,
        "owner_id": actor_a,
        "credential_id": "synthetic-credential",
        "status": "queued",
        "pid": 0,
    }
    jobs = source_workspace / "jobs"
    jobs.mkdir(parents=True)
    (jobs / f"{queued_job['id']}.json").write_text(
        json.dumps(queued_job, sort_keys=True), encoding="utf-8"
    )

    dump_result = _owned_docker(
        pg,
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "-U",
        "postgres",
        "-d",
        pg.database,
    )
    assert dump_result.returncode == 0, dump_result.stderr.decode(errors="replace")
    assert dump_result.stdout.startswith(b"PGDMP")

    backup = tmp_path / "backup"
    backup.mkdir()
    manifest = _write_backup(source_vault, source_workspace, backup, dump_result.stdout)
    backup_bytes = sum((backup / relative).stat().st_size for relative in manifest)

    corrupt = tmp_path / "corrupt-backup"
    shutil.copytree(backup, corrupt)
    with (corrupt / "database.dump").open("ab") as stream:
        stream.write(b"corrupt")
    refused_destination = tmp_path / "must-not-restore"
    with pytest.raises(ValueError, match="checksum mismatch"):
        _restore_files(corrupt, refused_destination)
    assert not refused_destination.exists()

    restore_started = time.monotonic()
    pg.reset()
    restore_result = _owned_docker(
        pg,
        "pg_restore",
        "--no-owner",
        "--exit-on-error",
        "-U",
        "postgres",
        "-d",
        pg.database,
        input_bytes=(backup / "database.dump").read_bytes(),
    )
    assert restore_result.returncode == 0, restore_result.stderr.decode(errors="replace")
    restored = tmp_path / "restored"
    _restore_files(backup, restored)
    elapsed_seconds = time.monotonic() - restore_started
    record_property("restore_sample_elapsed_seconds", round(elapsed_seconds, 6))
    record_property("restore_sample_backup_bytes", backup_bytes)
    print(
        f"restore_sample elapsed_seconds={elapsed_seconds:.6f} "
        f"backup_bytes={backup_bytes} synthetic_only=true"
    )

    with psycopg.connect(pg.dsn(role="postgres")) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT relname, relrowsecurity, relforcerowsecurity "
                "FROM pg_class WHERE relname = ANY(%s) ORDER BY relname",
                [["notes", "note_chunks", "figures"]],
            )
            assert cursor.fetchall() == [
                ("figures", True, True),
                ("note_chunks", True, True),
                ("notes", True, True),
            ]

    expected_a = {private_a, shared}
    expected_b = {private_b, shared}
    for table, column in (
        ("notes", "path"),
        ("note_chunks", "note_path"),
        ("figures", "note_path"),
    ):
        assert _visible_paths(pg, actor_a, table, column) == expected_a
        assert _visible_paths(pg, actor_b, table, column) == expected_b

    restored_vault = restored / "vault"
    restored_workspace = restored / "workspace"
    assert json.loads((restored_vault / ".article-identities.json").read_text()) == registry
    metadata = parse_frontmatter((restored_vault / shared).read_text(encoding="utf-8"))
    assert metadata["uploaded_by"] == actor_a
    assert json.loads(metadata["contributor_ids"]) == contributors
    assert json.loads(
        (restored_workspace / "jobs" / f"{queued_job['id']}.json").read_text()
    ) == queued_job
    for relative, expected_hash in manifest.items():
        if relative.startswith("files/vault/"):
            restored_path = restored_vault / relative.removeprefix("files/vault/")
        elif relative.startswith("files/workspace/"):
            restored_path = restored_workspace / relative.removeprefix(
                "files/workspace/"
            )
        else:
            continue
        assert _sha256(restored_path) == expected_hash
