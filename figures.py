"""
figures.py — Phase 4A: Extract and analyse figures from saved articles.

Flow per article:
  1. Parse saved .md for image references
  2. Resolve relative URLs using frontmatter source:
  3. Download images → ~/.second-brain/figures/{slug}/fig_{n}.png
  4. Analyse with Claude vision API (OCR + semantic description)
  5. Store in DuckDB figures table
"""

import base64
import hashlib
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

import vault_db

FIGURES_DIR = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "Library/CloudStorage/GoogleDrive-u9013039@gmail.com/我的雲端硬碟/PJ_save/second-brain"
)).expanduser().resolve() / ".figures"
IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_source_url(md_text: str) -> str | None:
    m = FRONTMATTER_RE.match(md_text)
    if not m:
        return None
    for line in m.group(1).splitlines():
        if line.startswith("source:"):
            val = line.split("source:", 1)[1].strip().strip('"').strip("'")
            return val if val.startswith("http") else None
    return None


def _resolve_url(img_path: str, source_url: str) -> str | None:
    """Turn a relative img path into an absolute URL."""
    if img_path.startswith("http"):
        return img_path
    if img_path.startswith("//"):
        return "https:" + img_path
    if source_url:
        return urljoin(source_url + "/", img_path)
    return None


def _slug(note_path: str) -> str:
    return hashlib.md5(note_path.encode()).hexdigest()[:12]


def _download_image(url: str, dest: Path) -> bool:
    """Download image to dest. Returns True on success."""
    if dest.exists():
        return True
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and r.content:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            return True
    except Exception:
        pass
    return False


def _image_to_base64(path: Path) -> str:
    return base64.standard_b64encode(path.read_bytes()).decode()


# ---------------------------------------------------------------------------
# VLM analysis via Claude API
# ---------------------------------------------------------------------------

def _analyse_with_claude(image_path: Path) -> dict:
    """Send image to Claude via anthropic SDK and get OCR + description."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        ext = image_path.suffix.lower().lstrip(".")
        media_type = f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
        if ext not in ("png", "jpg", "jpeg", "gif", "webp"):
            media_type = "image/png"

        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": _image_to_base64(image_path),
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Analyse this scientific figure. Respond in JSON with two fields:\n"
                            '{"ocr_text": "all text visible in the figure (labels, axes, legends, values)", '
                            '"description": "one sentence describing what this figure shows"}'
                        ),
                    },
                ],
            }],
        )
        import json
        raw = message.content[0].text.strip()
        # Extract JSON even if wrapped in markdown code block
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if json_match:
            data = json.loads(json_match.group())
            return {
                "ocr_text": data.get("ocr_text", ""),
                "description": data.get("description", ""),
            }
    except Exception:
        pass
    return {"ocr_text": "", "description": ""}


def _analyse_with_gemini(image_path: Path) -> dict:
    """Primary: Gemini CLI with image passed via stdin prompt + positional path."""
    try:
        prompt = (
            'Analyse this scientific figure. '
            'Reply ONLY in JSON with two fields: '
            '{"ocr_text": "all text visible in figure including labels axes legends values", '
            '"description": "one sentence describing what this figure shows"}'
        )
        result = subprocess.run(
            ["gemini", "--yolo", "--output-format", "text", "-", str(image_path)],
            input=prompt,
            capture_output=True, text=True, timeout=60,
            cwd=str(FIGURES_DIR.parent),  # run from vault root so path is in workspace
        )
        if result.returncode == 0 and result.stdout.strip():
            import json
            raw = result.stdout.strip()
            json_match = re.search(r"\{.*\}", raw, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
                return {
                    "ocr_text": data.get("ocr_text", ""),
                    "description": data.get("description", ""),
                }
    except Exception:
        pass
    return {"ocr_text": "", "description": ""}


def analyse_figure(image_path: Path) -> dict:
    """Claude → Gemini fallback."""
    result = _analyse_with_claude(image_path)
    if result["ocr_text"] or result["description"]:
        return result
    return _analyse_with_gemini(image_path)


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

_SKIP_PATTERNS = (
    "doubleclick", "pubads", "advertisement", "logo", "banner",
    "header", "footer", "icon", "avatar", "badge", "svg",
)


def _is_content_image(alt: str, url: str) -> bool:
    """Return True if this image is likely a content figure, not UI chrome."""
    combined = (alt + url).lower()
    return not any(p in combined for p in _SKIP_PATTERNS)


def extract_figures(note_path: str, vault: Path) -> list[dict]:
    """
    Extract and analyse all figures from a saved markdown article.
    Returns list of figure dicts with ocr_text and description.
    """
    md_file = vault / note_path
    if not md_file.exists():
        return []

    md_text = md_file.read_text(encoding="utf-8")
    source_url = _parse_source_url(md_text)
    matches = IMG_RE.findall(md_text)

    content_imgs = [
        (alt, url) for alt, url in matches
        if _is_content_image(alt, url)
    ]

    fig_dir = FIGURES_DIR / _slug(note_path)
    fig_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, (alt, img_ref) in enumerate(content_imgs):
        abs_url = _resolve_url(img_ref, source_url or "")
        if not abs_url:
            continue

        ext = Path(urlparse(abs_url).path).suffix or ".png"
        local = fig_dir / f"fig_{i:03d}{ext}"

        if not _download_image(abs_url, local):
            continue

        analysis = analyse_figure(local)
        token_est = SNAPSHOT_TIERS["base"]["token_est"]

        vault_db.upsert_figure(
            note_path=note_path,
            fig_index=i,
            image_url=abs_url,
            local_path=str(local),
            ocr_text=analysis["ocr_text"],
            description=analysis["description"],
            token_est=token_est,
        )

        results.append({
            "fig_index": i,
            "local_path": str(local),
            "ocr_text": analysis["ocr_text"],
            "description": analysis["description"],
        })

    return results


def process_article(note_path: str, vault: Path) -> str:
    """Extract figures for one article and return summary string."""
    figs = extract_figures(note_path, vault)
    if not figs:
        return f"No figures extracted from {note_path}"
    ok = [f for f in figs if f["description"]]
    return f"Extracted {len(figs)} figures, analysed {len(ok)} from {note_path}"


# ---------------------------------------------------------------------------
# Phase 4B: Note → PNG snapshot rendering
# ---------------------------------------------------------------------------

# Resolution tiers matching DeepSeek-OCR paper
SNAPSHOT_TIERS = {
    "large": {"width": 1280, "height": 1280, "token_est": 400},
    "base":  {"width": 1024, "height": 1024, "token_est": 256},
    "small": {"width":  640, "height":  640, "token_est": 100},
}

SNAPSHOTS_DIR = FIGURES_DIR.parent / ".snapshots"

_MD_CSS = """
body { font-family: -apple-system, sans-serif; font-size: 14px;
       line-height: 1.6; padding: 24px; max-width: 900px; margin: 0 auto; }
h1, h2, h3 { border-bottom: 1px solid #eee; padding-bottom: 4px; }
code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; }
pre  { background: #f4f4f4; padding: 12px; border-radius: 6px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 8px; }
blockquote { border-left: 3px solid #ccc; margin: 0; padding-left: 12px; color: #666; }
"""


def _md_to_html(md_text: str) -> str:
    """Convert markdown body (after frontmatter) to HTML."""
    fm_match = FRONTMATTER_RE.match(md_text)
    body = md_text[fm_match.end():] if fm_match else md_text
    try:
        import markdown2
        html_body = markdown2.markdown(body, extras=["tables", "fenced-code-blocks"])
    except ImportError:
        html_body = f"<pre>{body}</pre>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>{_MD_CSS}</style></head>
<body>{html_body}</body></html>"""


def render_note_to_png(
    note_path: str,
    vault: Path,
    tier: str = "base",
) -> Path | None:
    """
    Render a markdown note to PNG using Playwright headless Chromium.

    Returns path to the PNG file, or None on failure.
    Tier determines resolution: large (1280px), base (1024px), small (640px).
    """
    md_file = vault / note_path
    if not md_file.exists():
        return None

    cfg = SNAPSHOT_TIERS.get(tier, SNAPSHOT_TIERS["base"])
    slug = _slug(note_path)
    out_dir = SNAPSHOTS_DIR / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"snapshot_{tier}.png"

    if out_path.exists():
        vault_db.update_snapshot(note_path, str(out_path), tier, cfg["token_est"])
        return out_path

    md_text = md_file.read_text(encoding="utf-8")
    html = _md_to_html(md_text)

    # Write temp HTML
    tmp_html = out_dir / "tmp.html"
    tmp_html.write_text(html, encoding="utf-8")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": cfg["width"], "height": cfg["height"]}
            )
            page.goto(f"file://{tmp_html.resolve()}")
            page.wait_for_timeout(500)
            page.screenshot(path=str(out_path), full_page=True)
            browser.close()
    except Exception:
        return None
    finally:
        tmp_html.unlink(missing_ok=True)

    if out_path.exists():
        vault_db.update_snapshot(note_path, str(out_path), tier, cfg["token_est"])
        return out_path
    return None


def snapshot_note(note_path: str, vault: Path, tier: str = "base") -> dict:
    """Render note to PNG and return info dict."""
    out = render_note_to_png(note_path, vault, tier)
    if not out:
        return {"success": False, "path": None}
    size_kb = out.stat().st_size // 1024
    return {
        "success": True,
        "path": str(out),
        "tier": tier,
        "token_est": SNAPSHOT_TIERS[tier]["token_est"],
        "size_kb": size_kb,
    }
