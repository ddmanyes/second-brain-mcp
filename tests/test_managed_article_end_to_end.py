"""Managed PDF intake through shared search, read, and page-image access.

This opt-in test uses the owned disposable PostgreSQL harness.  Source resolution is
local and deterministic; PDF text extraction and pdftoppm page rendering are real.
Only the two model providers are replaced with deterministic callbacks.
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from mcp_second_brain import late_chunking, server, vault_db
from mcp_second_brain.identity import Identity
from mcp_second_brain.intake_identity import IntakeIdentityBridge
from tests.test_multiuser_rls import _as, two_members  # noqa: F401

queue_module = pytest.importorskip("lcdda_ingest.queue")
runtime_module = pytest.importorskip("lcdda_ingest.managed_runtime")
worker_module = pytest.importorskip("lcdda_ingest.managed_worker")
resolver_module = pytest.importorskip("lcdda_ingest.source_resolver")
workspace_module = pytest.importorskip("lcdda_ingest.workspace")

DurableQueue = queue_module.DurableQueue
ManagedSettings = runtime_module.ManagedSettings
run_job = worker_module.run_job
ResolvedArticle = resolver_module.ResolvedArticle
HarvestWorkspace = workspace_module.HarvestWorkspace

PDFTOPPM = Path(
    "/Users/lab_center/.cache/codex-runtimes/codex-primary-runtime/"
    "dependencies/bin/override/pdftoppm"
)
TAIL_QUERY = "managedendtoendtailmarker"


def _vector(seed: int) -> list[float]:
    return [1.0 if index == seed else 0.0 for index in range(1024)]


def _text_pdf(*lines: str) -> bytes:
    """Build one valid text PDF without a PDF-generation dependency."""

    commands = [b"BT /F1 11 Tf 72 720 Td"]
    for index, line in enumerate(lines):
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        if index:
            commands.append(b"0 -18 Td")
        commands.append(f"({escaped}) Tj".encode("ascii"))
    commands.append(b"ET")
    stream = b"\n".join(commands)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>\nstream\n"
        + stream
        + b"\nendstream",
    ]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(number).encode("ascii") + b" 0 obj\n" + value + b"\nendobj\n"
    xref = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(
        f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]
    )
    return (
        data
        + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
        + str(xref).encode("ascii")
        + b"\n%%EOF\n"
    )


@pytest.mark.skipif(not PDFTOPPM.is_file(), reason="bundled pdftoppm unavailable")
def test_managed_pdf_is_shared_end_to_end(two_members, tmp_path, monkeypatch):
    pytest.importorskip("pdfminer")
    vault = two_members["vault"]
    store = two_members["store"]
    workspace = HarvestWorkspace(tmp_path / "managed-workspace")
    credential_id = "c" * 64
    store.register_api_key(
        credential_id,
        "managed-member-a",
        "member",
        user_uuid=two_members["a_uuid"],
    )
    actor = store.get_identity_for_key(credential_id)
    assert isinstance(actor, Identity)
    assert actor.user_uuid == two_members["a_uuid"]
    assert actor.credential_id == credential_id

    bridge = IntakeIdentityBridge(store.get_identity_for_key)
    queue = DurableQueue(
        workspace,
        actor_provider=bridge.actor,
        verifier=bridge.verify,
        execution_guard=lambda: True,
    )
    with _as(actor):
        job = queue.enqueue(
            "ingest",
            {
                "source": "https://example.test/managed-end-to-end.pdf",
                "source_kind": "auto",
                "dry_run": False,
                "figures": "off",
            },
            [],
            None,
        )
    dispatched = queue.dispatch_once(lambda _: os.getpid())
    assert dispatched is not None and dispatched["id"] == job["id"]

    pdf = _text_pdf(
        "Managed End To End Paper",
        *("Opening shared evidence paragraph" for _ in range(30)),
        f"Final evidence contains {TAIL_QUERY}",
    )

    class Resolver:
        def resolve(self, source, staging):
            staging.mkdir(parents=True)
            pdf_path = staging / "source.pdf"
            pdf_path.write_bytes(pdf)
            digest = hashlib.sha256(pdf).hexdigest()
            return ResolvedArticle(
                "resolved",
                "",
                source_path=pdf_path,
                title="Managed End To End Paper",
                identity=f"sha256:{digest}",
                acquisition="remote_pdf",
                metadata={
                    "target_stem": "2026_Test_ManagedEndToEnd",
                    "canonical_url": source,
                    "sha256": digest,
                },
            )

    model_calls = {"embedding": 0, "chunks": 0}

    def deterministic_embedding(text):
        model_calls["embedding"] += 1
        # Search remains keyword-only, so no reranker or model endpoint is reached.
        if text == TAIL_QUERY:
            return None
        assert "Managed End To End Paper" in text
        assert TAIL_QUERY in text
        return _vector(1)

    def deterministic_chunks(body):
        model_calls["chunks"] += 1
        assert "Managed End To End Paper" in body
        assert TAIL_QUERY in body
        return [
            (body[:400], _vector(2)),
            (f"Final evidence contains {TAIL_QUERY}", _vector(3)),
        ]

    monkeypatch.setattr(vault_db, "embed_text", deterministic_embedding)
    monkeypatch.setattr(late_chunking, "chunk_and_embed", deterministic_chunks)
    monkeypatch.setattr(vault_db, "EMBED_AUTO_START", False)
    monkeypatch.setattr(vault_db, "EMBED_URL", "http://127.0.0.1:1/v1/embeddings")
    monkeypatch.setattr(
        late_chunking,
        "LATE_CHUNK_URL",
        "http://127.0.0.1:1/embedding",
    )

    settings = ManagedSettings(
        workspace=workspace.root,
        vault=vault,
        dsn="owned-disposable-postgres-is-already-open",
        local_enrichment=True,
        pdf_renderer=PDFTOPPM,
    )
    result = run_job(
        settings,
        job["id"],
        store,
        resolver_factory=Resolver,
    )

    rel = "20-areas/research/2026_Test_ManagedEndToEnd.md"
    assert result["status"] == "succeeded"
    assert result["saved_path"] == rel
    assert result["readiness"] == {
        "document": "complete",
        "text_index": "complete",
        "vectors": "complete",
        "chunks": "complete",
        "figures": "complete",
    }
    assert model_calls == {"embedding": 1, "chunks": 1}
    assert not workspace.lease_path.exists()
    assert (workspace.root / "remote-ingest" / job["id"] / "source.pdf").is_file()

    member_b = two_members["identity_b"]
    with _as(member_b):
        article_search = server.search_notes(TAIL_QUERY)
        article_text = server.read_note(rel)
        figure_search = server.search_figures("Page 1")
        figure = server.read_figure(rel, 0)
        private_search = server.search_notes("alphasecrettopic")
        private_read = server.read_note(two_members["a_note"])
        with store._conn() as connection:
            note = connection.execute(
                "SELECT owner_id, embedding IS NOT NULL FROM notes WHERE path = %s",
                [rel],
            ).fetchone()
            chunks = connection.execute(
                "SELECT content_hash, owner_id FROM note_chunks "
                "WHERE note_path = %s ORDER BY chunk_idx",
                [rel],
            ).fetchall()
            figures = connection.execute(
                "SELECT owner_id FROM figures WHERE note_path = %s ORDER BY fig_index",
                [rel],
            ).fetchall()

    assert "Managed End To End Paper" in article_search
    assert TAIL_QUERY in article_text
    assert rel in figure_search
    assert not isinstance(figure, str)
    assert "Alpha Secret Topic" not in private_search
    assert "must be within the vault" in private_read
    assert note == (None, True)
    assert len(chunks) == 2 and all(row[1] is None for row in chunks)
    assert figures == [(None,)]
