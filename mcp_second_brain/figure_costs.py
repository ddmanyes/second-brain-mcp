"""Read-only upper-bound estimator for figure backfill model usage."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

from .figures import _estimate_image_tokens

PRICING = {
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
OCR_PROMPT_INPUT_TOKENS = 64
OCR_OUTPUT_TOKEN_CAP = 1024
DETECTION_PROMPT_INPUT_TOKENS = 256
DETECTION_OUTPUT_TOKEN_CAP = 1024
PDF_PAGE_CAP = 20
PDF_RENDER_DPI = 150
VISION_LONG_EDGE_CAP = 2576
UNKNOWN_FIGURE_IMAGE_TOKENS = int((VISION_LONG_EDGE_CAP / 28) ** 2)
_SOURCE_PDF_RE = re.compile(
    r"\|\s*\*\*Source PDF\*\*\s*\|\s*([^|\n]+?)\s*\|",
    re.IGNORECASE,
)
_PRIORITY_RE = re.compile(
    r"^(?:priority|importance):\s*[\"']?([^\s\"']+)", re.IGNORECASE | re.MULTILINE
)
_PRIORITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


def _empty_category() -> dict:
    return {
        "notes": 0,
        "images": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "usd_upper_bound": 0.0,
    }


def _haiku_cost(input_tokens: int, output_tokens: int) -> float:
    return round(
        (
            input_tokens * PRICING["haiku"]["input_per_million"]
            + output_tokens * PRICING["haiku"]["output_per_million"]
        )
        / 1_000_000,
        6,
    )


def _model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICING[model]
    return round(
        (
            input_tokens * rates["input_per_million"]
            + output_tokens * rates["output_per_million"]
        )
        / 1_000_000,
        6,
    )


def _source_pdf(note_file: Path) -> Path | None:
    if not note_file.is_file():
        return None
    match = _SOURCE_PDF_RE.search(note_file.read_text(encoding="utf-8"))
    if not match:
        return None
    path = Path(match.group(1).strip()).expanduser()
    return path.resolve() if path.is_file() and path.suffix.lower() == ".pdf" else None


def _scaled_image_tokens(width: float, height: float) -> int:
    width = int(width)
    height = int(height)
    if max(width, height) > VISION_LONG_EDGE_CAP:
        scale = VISION_LONG_EDGE_CAP / max(width, height)
        width, height = int(width * scale), int(height * scale)
    return max(1, int((width / 28) * (height / 28)))


def _pdf_page_tokens(pdf_path: Path) -> tuple[int, list[int]]:
    """Return source page count and capped rendered-page token estimates."""
    try:
        import pypdfium2 as pdfium

        document = pdfium.PdfDocument(str(pdf_path))
        source_pages = len(document)
        tokens = []
        for page_index in range(min(source_pages, PDF_PAGE_CAP)):
            page = document[page_index]
            width, height = page.get_size()
            tokens.append(
                _scaled_image_tokens(
                    width * PDF_RENDER_DPI / 72,
                    height * PDF_RENDER_DPI / 72,
                )
            )
            page.close()
        document.close()
        return source_pages, tokens
    except Exception:  # noqa: BLE001 — optional PDF backends vary by runtime
        try:
            import fitz

            document = fitz.open(pdf_path)
            source_pages = document.page_count
            tokens = [
                _scaled_image_tokens(
                    document[index].rect.width * PDF_RENDER_DPI / 72,
                    document[index].rect.height * PDF_RENDER_DPI / 72,
                )
                for index in range(min(source_pages, PDF_PAGE_CAP))
            ]
            document.close()
            return source_pages, tokens
        except Exception:  # noqa: BLE001 — unreadable PDF remains unpriced
            return 0, []


def _query_hits(vault: Path) -> dict[str, int]:
    hits: dict[str, int] = {}
    log = vault / ".query-log.jsonl"
    if not log.is_file():
        return hits
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for path in row.get("results", []):
            if isinstance(path, str):
                hits[path] = hits.get(path, 0) + 1
    return hits


def _priority_order(note_paths: list[str], vault: Path, store) -> list[str]:
    query_hits = _query_hits(vault)
    ranked: list[tuple[int, int, int, str]] = []
    for note_path in dict.fromkeys(note_paths):
        rows = store.get_figures_for_note(note_path)
        if not any(
            not any(
                str(row.get(field) or "").strip()
                for field in ("ocr_text", "description", "caption")
            )
            for row in rows
        ):
            continue
        note_file = (vault / note_path).resolve()
        note_text = ""
        if note_file.is_relative_to(vault) and note_file.is_file():
            note_text = note_file.read_text(encoding="utf-8")
        match = _PRIORITY_RE.search(note_text)
        importance = _PRIORITY_WEIGHT.get(
            match.group(1).lower() if match else "", 0
        )
        captioned_rows = sum(bool(str(row.get("caption") or "").strip()) for row in rows)
        ranked.append((importance, query_hits.get(note_path, 0), captioned_rows, note_path))
    ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]))
    return [item[3] for item in ranked]


def _scenario_summary(note_paths: list[str], report: dict) -> dict:
    categories = report["categories"]
    existing = categories["ocr_only_existing_image"]
    remote = categories["remote_image_ocr"]
    pdf = categories["pdf_detection_and_ocr"]
    return {
        "selected_notes": len(note_paths),
        "selected_note_paths": note_paths,
        "images": existing["images"] + remote["images"] + pdf["images"],
        "pdf_pages": pdf["pages"],
        "haiku_input_tokens": (
            existing["input_tokens"]
            + remote["input_tokens"]
            + pdf["haiku_input_tokens"]
        ),
        "haiku_output_tokens": (
            existing["output_tokens"]
            + remote["output_tokens"]
            + pdf["haiku_output_tokens"]
        ),
        "sonnet_input_tokens": pdf["sonnet_input_tokens"],
        "sonnet_output_tokens": pdf["sonnet_output_tokens"],
        "usd_upper_bound": round(
            sum(category["usd_upper_bound"] for category in categories.values()), 6
        ),
    }


def estimate_figure_backfill_cost(
    note_paths: list[str], vault: Path, store, *, _include_scenarios: bool = True
) -> dict:
    """Estimate an upper bound without calling a model or changing the vault."""
    vault = vault.expanduser().resolve()
    existing = _empty_category()
    existing_notes: set[str] = set()
    remote = _empty_category()
    remote_notes: set[str] = set()
    missing_by_note: dict[str, list[dict]] = {}

    for note_path in dict.fromkeys(note_paths):
        for row in store.get_figures_for_note(note_path):
            if any(
                str(row.get(field) or "").strip()
                for field in ("ocr_text", "description", "caption")
            ):
                continue
            local_path = Path(str(row.get("local_path") or "")).expanduser()
            if not local_path.is_absolute():
                local_path = vault / local_path
            if not local_path.is_file():
                if urlparse(str(row.get("image_url") or "")).scheme in {
                    "http",
                    "https",
                }:
                    remote_notes.add(note_path)
                    remote["images"] += 1
                    remote["input_tokens"] += (
                        UNKNOWN_FIGURE_IMAGE_TOKENS + OCR_PROMPT_INPUT_TOKENS
                    )
                    remote["output_tokens"] += OCR_OUTPUT_TOKEN_CAP
                else:
                    missing_by_note.setdefault(note_path, []).append(row)
                continue
            existing_notes.add(note_path)
            existing["images"] += 1
            existing["input_tokens"] += (
                _estimate_image_tokens(local_path) + OCR_PROMPT_INPUT_TOKENS
            )
            existing["output_tokens"] += OCR_OUTPUT_TOKEN_CAP

    existing["notes"] = len(existing_notes)
    existing["usd_upper_bound"] = _haiku_cost(
        existing["input_tokens"], existing["output_tokens"]
    )
    remote["notes"] = len(remote_notes)
    remote["usd_upper_bound"] = _haiku_cost(
        remote["input_tokens"], remote["output_tokens"]
    )
    pdf_category = {
        "notes": 0,
        "images": 0,
        "pages": 0,
        "source_pages": 0,
        "page_cap": PDF_PAGE_CAP,
        "capped_notes": 0,
        "sonnet_input_tokens": 0,
        "sonnet_output_tokens": 0,
        "haiku_input_tokens": 0,
        "haiku_output_tokens": 0,
        "usd_upper_bound": 0.0,
    }
    for note_path, rows in missing_by_note.items():
        note_file = (vault / note_path).resolve()
        if not note_file.is_relative_to(vault):
            continue
        pdf_path = _source_pdf(note_file)
        if not pdf_path:
            continue
        source_pages, page_tokens = _pdf_page_tokens(pdf_path)
        if not page_tokens:
            continue
        pdf_category["notes"] += 1
        pdf_category["images"] += len(rows)
        pdf_category["pages"] += len(page_tokens)
        pdf_category["source_pages"] += source_pages
        pdf_category["capped_notes"] += int(source_pages > PDF_PAGE_CAP)
        pdf_category["sonnet_input_tokens"] += sum(page_tokens) + (
            len(page_tokens) * DETECTION_PROMPT_INPUT_TOKENS
        )
        pdf_category["sonnet_output_tokens"] += (
            len(page_tokens) * DETECTION_OUTPUT_TOKEN_CAP
        )
        pdf_category["haiku_input_tokens"] += len(rows) * (
            UNKNOWN_FIGURE_IMAGE_TOKENS + OCR_PROMPT_INPUT_TOKENS
        )
        pdf_category["haiku_output_tokens"] += len(rows) * OCR_OUTPUT_TOKEN_CAP

    pdf_category["usd_upper_bound"] = round(
        _model_cost(
            "sonnet",
            pdf_category["sonnet_input_tokens"],
            pdf_category["sonnet_output_tokens"],
        )
        + _model_cost(
            "haiku",
            pdf_category["haiku_input_tokens"],
            pdf_category["haiku_output_tokens"],
        ),
        6,
    )
    report = {
        "pricing": PRICING,
        "categories": {
            "ocr_only_existing_image": existing,
            "remote_image_ocr": remote,
            "pdf_detection_and_ocr": pdf_category,
        },
    }
    if not _include_scenarios:
        return report

    priority_order = _priority_order(note_paths, vault, store)
    scenarios = {}
    for name, limit in (("top_5_notes", 5), ("top_20_notes", 20), ("all_notes", None)):
        selected = priority_order if limit is None else priority_order[:limit]
        selected_report = estimate_figure_backfill_cost(
            selected, vault, store, _include_scenarios=False
        )
        scenarios[name] = _scenario_summary(selected, selected_report)
    return {
        **report,
        "candidate_notes": len(priority_order),
        "priority_order": priority_order,
        "scenarios": scenarios,
    }
