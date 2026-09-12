# ruff: noqa: F811
import hashlib
from uuid import UUID

import pytest
from PIL import Image

from mcp_second_brain import server
from mcp_second_brain.article_attribution import commit_shared_article
from mcp_second_brain.article_page_images import publish_article_pages
from mcp_second_brain.identity import Identity
from mcp_second_brain.note_row import project_note
from tests.test_multiuser_rls import _as, two_members  # noqa: F401


def test_uploaded_article_pages_are_shared_without_private_leak(two_members, tmp_path):
    vault, store = two_members["vault"], two_members["store"]
    actor = two_members["identity_a"]
    rel = "20-areas/research/2026_Test_VisualPaper.md"
    with _as(actor):
        committed = commit_shared_article(
            vault,
            rel,
            "---\ntitle: VisualPaper\ntype: research\n---\nShared visual evidence.",
            actor,
            source="https://example.test/visual",
            index=lambda path: store.index_shared_article_metadata(vault, path),
        )
    assert committed.index_status == "complete"
    staging = tmp_path / "staging"
    staging.mkdir()
    image = staging / "page.png"
    Image.new("RGB", (32, 32), "white").save(image)
    pages = [
        {
            "index": 1,
            "relative_staging_path": "page.png",
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "mime": "image/png",
        }
    ]
    expected_hash = project_note(vault, vault / rel).content_hash
    result = publish_article_pages(
        store,
        vault,
        rel,
        staging,
        pages,
        expected_hash=expected_hash,
        authorize=lambda: actor,
    )
    assert result == {"status": "complete", "count": 1, "kind": "document_pages"}

    with store._conn() as conn:
        conn.execute(
            "UPDATE figures SET image_url=%s, ocr_text=%s, token_est=%s "
            "WHERE note_path=%s AND fig_index=0",
            ["https://example.test/stale", "stale OCR", 91, rel],
        )
    assert (
        publish_article_pages(
            store,
            vault,
            rel,
            staging,
            pages,
            expected_hash=expected_hash,
            authorize=lambda: actor,
        )
        == result
    )
    figure = store.get_figure(rel, 0)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    expected_local_path = (
        vault
        / ("figures/managed-" + hashlib.sha256(rel.encode()).hexdigest()[:20])
        / f"fig-000-{digest[:12]}.png"
    )
    assert figure == {
        "note_path": rel,
        "fig_index": 0,
        "image_url": "",
        "local_path": str(expected_local_path),
        "ocr_text": "",
        "description": "Document page 1",
        "caption": "Page 1",
        "token_est": 0,
    }
    with _as(two_members["identity_b"]):
        assert "Shared visual evidence" in server.read_note(rel)
        assert "VisualPaper" in server.search_notes("VisualPaper")
        assert "Page 1" in server.search_figures("Page")
        result = server.read_figure(rel, 0)
        assert not isinstance(result, str)
        with store._conn() as conn:
            assert conn.execute(
                "SELECT owner_id FROM figures WHERE note_path=%s", [rel]
            ).fetchone() == (None,)
    with pytest.raises(ValueError, match="content changed"):
        publish_article_pages(
            store,
            vault,
            rel,
            staging,
            pages,
            expected_hash="stale",
            authorize=lambda: actor,
        )


def test_private_existing_page_row_is_not_made_shared(two_members, tmp_path):
    vault, store = two_members["vault"], two_members["store"]
    actor = two_members["identity_a"]
    rel = "20-areas/research/2026_Test_PrivatePageRow.md"
    with _as(actor):
        committed = commit_shared_article(
            vault,
            rel,
            "---\ntitle: PrivatePageRow\ntype: research\n---\nShared article.",
            actor,
            source="https://example.test/private-page-row",
            index=lambda path: store.index_shared_article_metadata(vault, path),
        )
    assert committed.index_status == "complete"
    staging = tmp_path / "private-row-staging"
    staging.mkdir()
    image = staging / "page.png"
    Image.new("RGB", (32, 32), "white").save(image)
    pages = [
        {
            "index": 1,
            "relative_staging_path": "page.png",
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "mime": "image/png",
        }
    ]
    expected_hash = project_note(vault, vault / rel).content_hash
    publish_article_pages(
        store,
        vault,
        rel,
        staging,
        pages,
        expected_hash=expected_hash,
        authorize=lambda: actor,
    )
    with two_members["admin_conn"].cursor() as cursor:
        cursor.execute(
            "UPDATE figures SET owner_id=%s, ocr_text=%s "
            "WHERE note_path=%s AND fig_index=0",
            [two_members["a_uuid"], "private marker", rel],
        )
    two_members["admin_conn"].commit()

    with pytest.raises(PermissionError, match="authorization denied"):
        publish_article_pages(
            store,
            vault,
            rel,
            staging,
            pages,
            expected_hash=expected_hash,
            authorize=lambda: actor,
        )
    with two_members["admin_conn"].cursor() as cursor:
        assert cursor.execute(
            "SELECT owner_id, ocr_text FROM figures WHERE note_path=%s AND fig_index=0",
            [rel],
        ).fetchone() == (UUID(two_members["a_uuid"]), "private marker")


def test_page_publication_pins_the_authorized_user(two_members, tmp_path):
    vault, store = two_members["vault"], two_members["store"]
    actor = two_members["identity_a"]
    rel = "20-areas/research/2026_Test_AuthSwitch.md"
    with _as(actor):
        committed = commit_shared_article(
            vault,
            rel,
            "---\ntitle: AuthSwitch\ntype: research\n---\nShared article.",
            actor,
            source="https://example.test/auth-switch",
            index=lambda path: store.index_shared_article_metadata(vault, path),
        )
    assert committed.index_status == "complete"
    staging = tmp_path / "auth-switch-staging"
    staging.mkdir()
    image = staging / "page.png"
    Image.new("RGB", (32, 32), "white").save(image)
    pages = [
        {
            "index": 1,
            "relative_staging_path": "page.png",
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "mime": "image/png",
        }
    ]
    identities = iter([actor, two_members["identity_b"]])

    with pytest.raises(PermissionError, match="authorization denied"):
        publish_article_pages(
            store,
            vault,
            rel,
            staging,
            pages,
            expected_hash=project_note(vault, vault / rel).content_hash,
            authorize=lambda: next(identities),
        )
    assert store.get_figures_for_note(rel) == []


def test_page_publication_rejects_invalid_identity_uuid(tmp_path):
    vault = tmp_path / "vault"
    staging = tmp_path / "staging"
    vault.mkdir()
    staging.mkdir()
    image = staging / "page.png"
    Image.new("RGB", (32, 32), "white").save(image)
    pages = [
        {
            "index": 1,
            "relative_staging_path": "page.png",
            "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
            "mime": "image/png",
        }
    ]
    invalid = Identity("invalid", "member", "not-a-uuid")

    with pytest.raises(PermissionError, match="authorization denied"):
        publish_article_pages(
            object(),
            vault,
            "20-areas/research/invalid.md",
            staging,
            pages,
            expected_hash="0" * 32,
            authorize=lambda: invalid,
        )
