import concurrent.futures
import json

import pytest

from mcp_second_brain.article_attribution import (
    article_identity,
    attribute_article,
    commit_shared_article,
    validate_shared_destination,
)
from mcp_second_brain.identity import Identity
from mcp_second_brain.note_row import parse_frontmatter


A = Identity("a", "member", "11111111-1111-1111-1111-111111111111")
B = Identity("b", "member", "22222222-2222-2222-2222-222222222222")
ARTICLE = '---\ntitle: Article\ntype: research\ntags: [research, uploaded-by-forged]\n---\n\nBody\n'


def test_shared_provenance_does_not_set_private_owner():
    result = attribute_article(ARTICLE, A, created=True)
    metadata = parse_frontmatter(result)
    assert metadata["uploaded_by"] == A.user_uuid
    assert "owner_id" not in metadata
    assert "uploaded-by-forged" not in result
    assert f"uploaded-by-{A.user_uuid}" in result
    assert result.endswith("Body\n")


def test_duplicate_contribution_is_idempotent_and_preserves_first_uploader():
    first = attribute_article(ARTICLE, A, created=True)
    second = attribute_article(first, B, created=False)
    assert attribute_article(second, B, created=False) == second
    metadata = parse_frontmatter(second)
    assert metadata["uploaded_by"] == A.user_uuid
    assert json.loads(metadata["contributor_ids"]) == [A.user_uuid, B.user_uuid]


def test_historical_first_uploader_is_not_guessed():
    assert "uploaded_by" not in parse_frontmatter(attribute_article(ARTICLE, A, created=False))


SOURCE = "https://example.org/paper"


def commit(vault, actor=A, rel="30-resources/article.md", **kwargs):
    return commit_shared_article(vault, rel, ARTICLE, actor, source=SOURCE,
                                 index=kwargs.pop("index", lambda path: True), **kwargs)


def test_new_conversion_cannot_forge_contributors():
    forged = ARTICLE.replace("type: research", 'type: research\ncontributor_ids: ["' + B.user_uuid + '"]')
    result = parse_frontmatter(attribute_article(forged, A, created=True))
    assert json.loads(result["contributor_ids"]) == [A.user_uuid]


def test_private_owner_cannot_be_made_shared():
    private = ARTICLE.replace("type: research", "type: research\nowner_id: " + A.user_uuid)
    with pytest.raises(ValueError, match="private ownership"):
        attribute_article(private, A, created=True)


@pytest.mark.parametrize("destination", [
    "90-personal/" + A.user_uuid, "decisions", "memory", "/30-resources",
    "30-resources/../90-personal", "30-resources\\private",
])
def test_shared_destination_boundary(destination):
    with pytest.raises(ValueError):
        validate_shared_destination(destination)


def test_canonical_article_identity():
    assert article_identity(SOURCE, "body", verified_metadata={"doi": "https://doi.org/10.1234/ABC"}) == "doi:10.1234/abc"
    assert article_identity(SOURCE, "body", verified_metadata={"pmid": "12345"}) == "pmid:12345"
    assert article_identity("https://EXAMPLE.org:443/paper#section", "body") == "url:" + SOURCE
    assert article_identity("", "body").startswith("sha256:")
    assert article_identity("", "body") != article_identity("", "different")


def test_same_article_different_names_contributors_merge(tmp_path):
    first = commit(tmp_path)
    second = commit(tmp_path, B, "20-areas/research/different.md")
    assert first.created and not second.created
    assert first.path == second.path
    assert not (tmp_path / "20-areas/research/different.md").exists()
    metadata = parse_frontmatter((tmp_path / first.path).read_text())
    assert json.loads(metadata["contributor_ids"]) == [A.user_uuid, B.user_uuid]
    assert metadata["uploaded_by"] == A.user_uuid
    assert "owner_id" not in metadata


def test_filename_collision_cannot_overwrite_other_source(tmp_path):
    saved = commit(tmp_path)
    before = (tmp_path / saved.path).read_bytes()
    with pytest.raises(FileExistsError):
        commit_shared_article(tmp_path, saved.path, ARTICLE, B,
                              source="https://example.org/different", index=lambda path: True)
    assert (tmp_path / saved.path).read_bytes() == before


def test_concurrent_submissions_keep_both_contributors(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda who: commit(tmp_path, who), [A, B]))
    assert sum(result.created for result in outcomes) == 1
    metadata = parse_frontmatter((tmp_path / outcomes[0].path).read_text())
    assert set(json.loads(metadata["contributor_ids"])) == {A.user_uuid, B.user_uuid}
    assert len(list((tmp_path / "30-resources").glob("*.md"))) == 1


def test_failed_index_is_durable_pending_and_retryable(tmp_path):
    def failed(path):
        raise RuntimeError("database unavailable")

    result = commit(tmp_path, index=failed)
    assert result.index_status == "pending"
    registry = json.loads((tmp_path / ".article-identities.json").read_text())
    assert registry["url:" + SOURCE]["index_status"] == "pending"
    resumed = commit(tmp_path, rel="30-resources/retry.md")
    assert resumed.path == result.path
    assert not resumed.created
    assert resumed.index_status == "complete"
    assert json.loads((tmp_path / ".article-identities.json").read_text())["url:" + SOURCE]["index_status"] == "complete"


def test_no_false_success_when_index_returns_none(tmp_path):
    assert commit(tmp_path, index=lambda path: None).index_status == "pending"


def test_reservation_survives_interrupted_file_publication(tmp_path, monkeypatch):
    from mcp_second_brain import article_attribution

    actual = article_attribution._atomic_write
    def interrupted(path, content):
        if path.suffix == ".md":
            raise OSError("interrupted")
        actual(path, content)
    with monkeypatch.context() as context:
        context.setattr(article_attribution, "_atomic_write", interrupted)
        with pytest.raises(OSError):
            commit(tmp_path)
    resumed = commit(tmp_path, rel="30-resources/another-name.md")
    assert resumed.path == "30-resources/article.md"
    assert resumed.created


def test_historical_matching_source_keeps_unknown_first_uploader(tmp_path):
    path = tmp_path / "30-resources/article.md"
    path.parent.mkdir()
    path.write_text(ARTICLE.replace("type: research", "type: research\nsource: " + SOURCE))
    commit(tmp_path)
    assert "uploaded_by" not in parse_frontmatter(path.read_text())


def test_symlink_cannot_put_shared_article_in_private_area(tmp_path):
    (tmp_path / "90-personal").mkdir()
    (tmp_path / "30-resources").symlink_to(tmp_path / "90-personal")
    with pytest.raises(ValueError):
        commit(tmp_path)


@pytest.mark.parametrize("source, folder, expected", [
    ("/etc/passwd", "30-resources", "server-local"),
    (SOURCE, "decisions", "shared"),
    (SOURCE, "30-resources", "temporarily unavailable"),
])
def test_member_intake_denies_unsafe_conversion_before_io(monkeypatch, source, folder, expected):
    from mcp_second_brain import server
    from mcp_second_brain.identity import _current, set_identity

    def forbidden(*args, **kwargs):
        raise AssertionError("must not convert or access source")

    monkeypatch.setattr(server, "_validate_source", forbidden)
    monkeypatch.setattr(server._md_converter, "convert", forbidden)
    token = set_identity(A)
    try:
        result = server.save_article(source, dest_folder=folder)
    finally:
        _current.reset(token)
    assert expected in result


def test_later_verified_doi_joins_existing_url_identity(tmp_path):
    original = commit(tmp_path)
    enriched = commit(tmp_path, B, rel="20-areas/research/doi-name.md",
                      verified_metadata={"doi": "10.1234/abc", "pmid": "12345"})
    assert enriched.path == original.path
    assert not enriched.created
    registry = json.loads((tmp_path / ".article-identities.json").read_text())
    assert registry["doi:10.1234/abc"]["path"] == original.path
    assert registry["pmid:12345"]["path"] == original.path


def test_verified_upload_hash_is_stable_across_body_rendering():
    from mcp_second_brain.article_attribution import article_identity
    digest = "b" * 64
    assert article_identity("", "first rendering", verified_metadata={"sha256": digest}) == "sha256:" + digest
    assert article_identity("", "second rendering", verified_metadata={"sha256": digest}) == "sha256:" + digest
    with pytest.raises(ValueError, match="source hash"):
        article_identity("", "body", verified_metadata={"sha256": "bad"})
