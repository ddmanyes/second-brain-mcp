"""Public-seam tests for the read-only figure backfill cost estimator."""

from pathlib import Path

from PIL import Image

from mcp_second_brain.figure_costs import estimate_figure_backfill_cost


class FakeStore:
    def __init__(self, rows: list[dict]):
        self.rows = rows

    def get_figures_for_note(self, note_path: str) -> list[dict]:
        return [row for row in self.rows if row["note_path"] == note_path]


def test_existing_empty_proxy_image_has_haiku_only_upper_bound(tmp_path: Path):
    note_path = "20-areas/research/paper.md"
    note = tmp_path / note_path
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: Important paper\nstatus: active\n---\n", encoding="utf-8")
    image = tmp_path / "figures/paper/fig-00.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (280, 280), "white").save(image)
    store = FakeStore([
        {
            "note_path": note_path,
            "fig_index": 0,
            "image_url": f"file://{image}",
            "local_path": str(image),
            "ocr_text": "",
            "description": "",
            "caption": "",
            "token_est": 0,
        }
    ])

    report = estimate_figure_backfill_cost([note_path], tmp_path, store)

    category = report["categories"]["ocr_only_existing_image"]
    assert category == {
        "notes": 1,
        "images": 1,
        "input_tokens": 164,
        "output_tokens": 1024,
        "usd_upper_bound": 0.005284,
    }
    assert report["pricing"] == {
        "as_of": "2026-09-09",
        "currency": "USD",
        "haiku": {
            "model": "claude-haiku-4-5-20251001",
            "input_per_million": 1.0,
            "output_per_million": 5.0,
        },
        "sonnet": {
            "model": "claude-sonnet-4-6",
            "input_per_million": 3.0,
            "output_per_million": 15.0,
        },
        "sources": {
            "haiku": "https://www.anthropic.com/news/claude-haiku-4-5",
            "sonnet": "https://www.anthropic.com/news/claude-sonnet-4-6",
        },
    }


def test_missing_figure_uses_source_pdf_with_twenty_page_detection_cap(tmp_path: Path):
    source_pdf = tmp_path / "source.pdf"
    pages = [Image.new("RGB", (280, 280), "white") for _ in range(25)]
    pages[0].save(source_pdf, "PDF", save_all=True, append_images=pages[1:])

    note_path = "20-areas/research/pdf-paper.md"
    note = tmp_path / note_path
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntitle: PDF paper\nstatus: active\n---\n\n"
        f"| **Source PDF** | {source_pdf} |\n",
        encoding="utf-8",
    )
    missing = tmp_path / "figures/pdf-paper/fig-00.png"
    store = FakeStore([
        {
            "note_path": note_path,
            "fig_index": 0,
            "image_url": f"file://{missing}",
            "local_path": str(missing),
            "ocr_text": "",
            "description": "",
            "caption": "",
            "token_est": 0,
        }
    ])

    report = estimate_figure_backfill_cost([note_path], tmp_path, store)

    category = report["categories"]["pdf_detection_and_ocr"]
    assert category["notes"] == 1
    assert category["images"] == 1
    assert category["pages"] == 20
    assert category["source_pages"] == 25
    assert category["page_cap"] == 20
    assert category["capped_notes"] == 1
    assert category["usd_upper_bound"] > 0


def test_remote_missing_image_is_estimated_without_downloading(tmp_path: Path):
    note_path = "20-areas/research/remote-paper.md"
    note = tmp_path / note_path
    note.parent.mkdir(parents=True)
    note.write_text("---\ntitle: Remote paper\n---\n", encoding="utf-8")
    store = FakeStore([
        {
            "note_path": note_path,
            "fig_index": 0,
            "image_url": "https://example.org/figure.png",
            "local_path": str(tmp_path / "figures/remote-paper/fig-00.png"),
            "ocr_text": "",
            "description": "",
            "caption": "",
            "token_est": 0,
        }
    ])

    report = estimate_figure_backfill_cost([note_path], tmp_path, store)

    assert report["categories"]["remote_image_ocr"] == {
        "notes": 1,
        "images": 1,
        "input_tokens": 8528,
        "output_tokens": 1024,
        "usd_upper_bound": 0.013648,
    }
    assert not (tmp_path / "figures/remote-paper/fig-00.png").exists()


def test_scenarios_rank_by_priority_then_query_hits_and_are_bounded(tmp_path: Path):
    rows = []
    note_paths = []
    for name in "abcdef":
        note_path = f"20-areas/research/{name}.md"
        note_paths.append(note_path)
        note = tmp_path / note_path
        note.parent.mkdir(parents=True, exist_ok=True)
        priority = "priority: high\n" if name == "f" else ""
        note.write_text(
            f"---\ntitle: {name}\n{priority}---\n", encoding="utf-8"
        )
        image = tmp_path / f"figures/{name}/fig-00.png"
        image.parent.mkdir(parents=True)
        Image.new("RGB", (280, 280), "white").save(image)
        rows.append({
            "note_path": note_path,
            "fig_index": 0,
            "image_url": f"file://{image}",
            "local_path": str(image),
            "ocr_text": "",
            "description": "",
            "caption": "",
            "token_est": 0,
        })
    (tmp_path / ".query-log.jsonl").write_text(
        '{"query":"x","results":["20-areas/research/e.md"]}\n'
        '{"query":"y","results":["20-areas/research/e.md"]}\n',
        encoding="utf-8",
    )

    report = estimate_figure_backfill_cost(note_paths, tmp_path, FakeStore(rows))

    assert report["priority_order"][:2] == [
        "20-areas/research/f.md",
        "20-areas/research/e.md",
    ]
    assert report["scenarios"]["top_5_notes"]["selected_notes"] == 5
    assert report["scenarios"]["top_5_notes"]["selected_note_paths"] == [
        "20-areas/research/f.md",
        "20-areas/research/e.md",
        "20-areas/research/a.md",
        "20-areas/research/b.md",
        "20-areas/research/c.md",
    ]
    assert report["scenarios"]["top_20_notes"]["selected_notes"] == 6
    assert report["scenarios"]["all_notes"]["selected_notes"] == 6


def test_empty_queue_has_zero_cost_scenarios(tmp_path: Path):
    report = estimate_figure_backfill_cost([], tmp_path, FakeStore([]))

    assert report["candidate_notes"] == 0
    assert report["priority_order"] == []
    assert report["scenarios"]["top_5_notes"]["selected_notes"] == 0
    assert report["scenarios"]["top_20_notes"]["usd_upper_bound"] == 0.0
    assert report["scenarios"]["all_notes"]["images"] == 0
