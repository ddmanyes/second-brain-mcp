"""
figures.py — Phase 4A: Extract and analyse figures from saved articles.

Flow per article:
  1. Parse saved .md for image references
  2. Resolve relative URLs using frontmatter source:
  3. Download images → {vault}/figures/{note-kebab}/fig-{NN}.png
     (visible dir so Obsidian indexes them; kebab slug from the note filename)
  4. Analyse with Claude vision API (OCR + semantic description)
  5. Store in DuckDB figures table
"""

import base64
import hashlib
import ipaddress
import os
import re
import socket
import sys
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from . import vault_db

# RFC-1918, loopback, link-local, AWS metadata — all off-limits for outbound fetches
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_ssrf_safe(url: str) -> bool:
    """Return False if the URL resolves to a private/loopback address."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
        return not any(
            ipaddress.ip_address(addr[4][0]) in net
            for addr in infos
            for net in _BLOCKED_NETS
        )
    except Exception:
        return False

# Visible (non-hidden) so Obsidian indexes extracted figures. snapshots stay
# hidden (.snapshots) since they are an internal vision-read cache, not content.
FIGURES_DIR = Path(os.environ.get(
    "SECOND_BRAIN_PATH",
    Path.home() / "second-brain"
)).expanduser().resolve() / "figures"
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
            return val
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
    # MD5 slug — used for .snapshots/ dirs (internal cache, matches vault_db._note_slug).
    return hashlib.md5(note_path.encode(), usedforsecurity=False).hexdigest()[:12]


def _figure_slug(note_path: str) -> str:
    """Human-readable kebab slug for a figure folder, derived from the note filename.

    e.g. "20-areas/research/2024_Smith_FooBar.md" -> "2024-smith-foobar".
    Visible figures/ + readable slug means Obsidian shows them and the folder
    name maps back to the source note. Mirrors server._slugify (punctuation
    becomes a separator, not removed). Falls back to the MD5 slug if the stem
    slugifies to empty (e.g. an all-punctuation filename).
    """
    stem = Path(note_path).stem.lower().strip()
    stem = re.sub(r"[^\w\s-]", " ", stem)       # punctuation -> space (don't glue words)
    stem = re.sub(r"[\s_]+", "-", stem).strip("-")
    return stem or _slug(note_path)


_MAX_IMAGE_BYTES = 20 * 1024 * 1024  # 20 MB


def _download_image(url: str, dest: Path) -> bool:
    """Download image to dest. Returns True on success."""
    if dest.exists():
        return True
    if not _is_ssrf_safe(url):
        return False
    try:
        with requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, stream=True) as r:
            if r.status_code != 200:
                return False
            if not r.headers.get("Content-Type", "").startswith("image/"):
                return False
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(8192):
                total += len(chunk)
                if total > _MAX_IMAGE_BYTES:
                    return False
                chunks.append(chunk)
            if not chunks:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"".join(chunks))
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
            max_tokens=1024,
            stream=False,
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
            ["gemini", "--output-format", "text", "-", str(image_path)],
            input=prompt,
            capture_output=True, text=True, timeout=60,
            cwd=str(FIGURES_DIR.parent),
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
# Phase 2: PDF page-render + VLM figure detection + crop
#
# pdfimages can only pull *embedded raster* images; vector figures (matplotlib
# charts, SVG-derived diagrams) are invisible to it. Instead we render each page
# to PNG, ask a VLM for figure bounding boxes, and crop. See IMPLEMENTATION_PLAN.
# ---------------------------------------------------------------------------

_DETECT_MODEL = "claude-sonnet-4-6"


def _render_pdf_pages(pdf_path: str, dpi: int = 150, max_pages: int = 20) -> list[Path]:
    """Render each PDF page to a PNG. Returns list of temp PNG paths (caller cleans up)."""
    import fitz
    import tempfile

    doc = fitz.open(pdf_path)
    out_dir = Path(tempfile.mkdtemp(prefix="sb-pages-"))
    paths: list[Path] = []
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
            p = out_dir / f"page-{i:03d}.png"
            pix.save(str(p))
            paths.append(p)
    finally:
        doc.close()
    return paths


def _detect_figures_on_page(page_png: Path, page_num: int) -> list[dict]:
    """Ask Claude (vision) for figures/tables on a rendered page.

    Returns a list of {"bbox": [x0,y0,x1,y1] in PIXELS, "caption": str, "type": str}.
    The VLM is asked for NORMALISED 0–1000 coordinates (VLMs calibrate relative
    coords far better than absolute pixels); we convert back to pixels here.
    """
    import json
    import anthropic
    from PIL import Image as _PILImage

    with _PILImage.open(page_png) as im:
        w, h = im.size

    client = anthropic.Anthropic()
    prompt = (
        f"This is page {page_num} of a scientific paper.\n"
        "Identify all figures, charts, diagrams, and tables (NOT body text paragraphs).\n"
        'Return ONLY a JSON array: [{"bbox": [x0, y0, x1, y1], "caption": "...", "type": "figure|table"}]\n'
        "bbox coordinates are NORMALISED 0-1000 from the top-left corner "
        "(x0,y0 = top-left, x1,y1 = bottom-right). caption is the figure/table "
        "caption text if visible, else empty. If there are no figures, return []."
    )
    message = client.messages.create(
        model=_DETECT_MODEL,
        max_tokens=1024,
        stream=False,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": _image_to_base64(page_png),
                }},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    raw = message.content[0].text.strip()
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    items = json.loads(m.group())
    out: list[dict] = []
    for it in items:
        bbox = it.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        px = [
            int(x0 / 1000 * w), int(y0 / 1000 * h),
            int(x1 / 1000 * w), int(y1 / 1000 * h),
        ]
        out.append({
            "bbox": px,
            "caption": (it.get("caption") or "").strip(),
            "type": (it.get("type") or "figure").strip(),
        })
    return out


def _crop_figure(page_png: Path, bbox: list, dest: Path) -> bool:
    """Crop bbox (pixels) out of page_png to dest. Returns False for tiny regions."""
    from PIL import Image as _PILImage

    with _PILImage.open(page_png) as img:
        w, h = img.size
        x0, y0, x1, y1 = (max(0, int(v)) for v in bbox)
        x1, y1 = min(x1, w), min(y1, h)
        if (x1 - x0) < 50 or (y1 - y0) < 50:  # skip tiny / degenerate regions
            return False
        cropped = img.crop((x0, y0, x1, y1))
        dest.parent.mkdir(parents=True, exist_ok=True)
        cropped.save(str(dest), "PNG")
    return True


def _estimate_image_tokens(image_path: Path) -> int:
    """Rough Anthropic-vision token estimate from pixel dims (~1 token per 28x28 patch)."""
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as im:
            w, h = im.size
        return max(1, int((w / 28) * (h / 28)))
    except Exception:
        return SNAPSHOT_TIERS["base"]["token_est"]


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


def _write_figure_section(note_path: str, fig_dir: Path, md_file: Path) -> int:
    """(Re)write the '## Extracted Figures' section from fig-*.png on disk.

    Driven by the files actually present (not just newly-added ones), so it is
    correct on cache-hit re-runs. Returns the number of figures linked.
    """
    figs = [p.name for p in sorted(fig_dir.glob("fig-*.png"))]
    if not figs or not md_file.exists():
        return 0
    content = md_file.read_text(encoding="utf-8").replace("\r\n", "\n")
    note_depth = len(Path(note_path).parent.parts)
    rel_prefix = "../" * note_depth
    fig_slug = _figure_slug(note_path)
    new_section = "\n\n## Extracted Figures\n" + "".join(
        f"![{fig}]({rel_prefix}figures/{fig_slug}/{fig})\n" for fig in figs
    )
    if "## Extracted Figures" in content:
        content = re.sub(r"\n*## Extracted Figures\n[\s\S]*", new_section, content)
        md_file.write_text(content, encoding="utf-8")
        print(f"[figures] Updated {len(figs)} figures in markdown: {md_file}", file=sys.stderr)
    else:
        with open(md_file, "a", encoding="utf-8") as f:
            f.write(new_section)
        print(f"[figures] Appended {len(figs)} figures to markdown: {md_file}", file=sys.stderr)
    return len(figs)


def _extract_figures_pdfimages(pdf_path: str, note_path: str, fig_dir: Path) -> list[dict]:
    """Legacy raster path: pdfimages -png (embedded raster only — no vector figures).

    Retained as the fallback for when VLM page-detection is unavailable.
    """
    import struct
    import shutil
    import glob

    temp_dir = fig_dir / "temp_extracted"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    pdfimages_path = shutil.which("pdfimages") or "/opt/homebrew/bin/pdfimages"
    cmd = [pdfimages_path, "-png", str(pdf_path), str(temp_dir / "img")]
    try:
        subprocess.run(cmd, check=True)
    except Exception as e:
        print(f"[figures] pdfimages failed: {e}", file=sys.stderr)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return []

    def get_png_size(filepath):
        try:
            with open(filepath, "rb") as f:
                data = f.read(24)
                if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
                    w, h = struct.unpack(">II", data[16:24])
                    return w, h
        except Exception:
            pass
        return 0, 0

    extracted_pngs = sorted(glob.glob(str(temp_dir / "*.png")))
    valid_count = 0
    results: list[dict] = []
    for png in extracted_pngs:
        w, h = get_png_size(png)
        if w > 200 and h > 200:
            local = fig_dir / f"fig-{valid_count:02d}.png"
            shutil.copy2(png, local)
            analysis = analyse_figure(local)
            vault_db.upsert_figure(
                note_path=note_path,
                fig_index=valid_count,
                image_url=f"file://{local.resolve()}",
                local_path=str(local),
                ocr_text=analysis["ocr_text"],
                description=analysis["description"],
                token_est=_estimate_image_tokens(local),
            )
            results.append({
                "fig_index": valid_count,
                "local_path": str(local),
                "ocr_text": analysis["ocr_text"],
                "description": analysis["description"],
            })
            valid_count += 1
    shutil.rmtree(temp_dir, ignore_errors=True)
    return results


def _extract_figures_render(pdf_path: str, note_path: str, fig_dir: Path) -> list[dict] | None:
    """Primary path: render pages → VLM bbox detection → crop. Captures vector figures.

    Returns a list of figure dicts on success (possibly empty), or None to signal
    "VLM detection unavailable" so the caller falls back to pdfimages.
    """
    import shutil

    try:
        pages = _render_pdf_pages(str(pdf_path))
    except Exception as e:
        print(f"[figures] page render failed: {e}", file=sys.stderr)
        return None
    if not pages:
        return []

    page_root = pages[0].parent
    fig_index = len(list(fig_dir.glob("fig-*.png")))
    results: list[dict] = []
    detection_ok = False
    detection_err = False
    try:
        for pnum, page_png in enumerate(pages):
            page_hash = hashlib.md5(page_png.read_bytes(), usedforsecurity=False).hexdigest()[:16]
            if vault_db.page_is_processed(note_path, page_hash):
                continue  # negative cache: page already seen (even if 0 figures)
            try:
                dets = _detect_figures_on_page(page_png, pnum)
            except Exception as e:
                print(f"[figures] VLM detect failed on page {pnum}: {e}", file=sys.stderr)
                detection_err = True
                continue  # transient — don't mark, so a later run retries
            detection_ok = True
            for det in dets:
                dest = fig_dir / f"fig-{fig_index:02d}.png"
                if not _crop_figure(page_png, det["bbox"], dest):
                    continue
                analysis = analyse_figure(dest)
                caption = det.get("caption", "")
                description = analysis["description"] or caption
                vault_db.upsert_figure(
                    note_path=note_path,
                    fig_index=fig_index,
                    image_url=f"file://{dest.resolve()}",
                    local_path=str(dest),
                    ocr_text=analysis["ocr_text"],
                    description=description,
                    token_est=_estimate_image_tokens(dest),
                    caption=caption,
                )
                results.append({
                    "fig_index": fig_index,
                    "local_path": str(dest),
                    "ocr_text": analysis["ocr_text"],
                    "description": description,
                    "caption": caption,
                })
                fig_index += 1
            vault_db.mark_page_processed(note_path, page_hash, len(dets))
    finally:
        shutil.rmtree(page_root, ignore_errors=True)

    # Every detection attempt failed and we produced nothing → let caller fall back.
    if detection_err and not detection_ok and not results:
        return None
    return results


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
    
    # Check if source_url is a local PDF file
    is_pdf = False
    if source_url:
        source_path = Path(source_url)
        if source_path.suffix.lower() == ".pdf" and source_path.exists():
            is_pdf = True

    if is_pdf:
        # PDF figures: render-based VLM detection (captures vector figures),
        # with pdfimages as the fallback when the VLM is unavailable.
        pdf_path = str(source_url)
        fig_dir = FIGURES_DIR / _figure_slug(note_path)
        fig_dir.mkdir(parents=True, exist_ok=True)

        results = _extract_figures_render(pdf_path, note_path, fig_dir)
        if results is None:
            print("[figures] render/VLM detection unavailable — falling back to pdfimages",
                  file=sys.stderr)
            results = _extract_figures_pdfimages(pdf_path, note_path, fig_dir)

        _write_figure_section(note_path, fig_dir, md_file)
        return results

    matches = IMG_RE.findall(md_text)

    content_imgs = [
        (alt, url) for alt, url in matches
        if _is_content_image(alt, url)
    ]

    fig_dir = FIGURES_DIR / _figure_slug(note_path)
    fig_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, (alt, img_ref) in enumerate(content_imgs):
        abs_url = _resolve_url(img_ref, source_url or "")
        if not abs_url:
            continue

        ext = Path(urlparse(abs_url).path).suffix or ".png"
        local = fig_dir / f"fig-{i:02d}{ext}"

        if not _download_image(abs_url, local):
            continue

        analysis = analyse_figure(local)

        vault_db.upsert_figure(
            note_path=note_path,
            fig_index=i,
            image_url=abs_url,
            local_path=str(local),
            ocr_text=analysis["ocr_text"],
            description=analysis["description"],
            token_est=_estimate_image_tokens(local),
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
    except ImportError:
        print(
            "[figures] playwright not installed — run: pip install playwright && playwright install chromium",
            file=sys.stderr,
        )
        return None
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
        return {
            "success": False,
            "path": None,
            "error": "Rendering failed — playwright may not be installed. Run: pip install playwright && playwright install chromium",
        }
    size_kb = out.stat().st_size // 1024
    return {
        "success": True,
        "path": str(out),
        "tier": tier,
        "token_est": SNAPSHOT_TIERS[tier]["token_est"],
        "size_kb": size_kb,
    }


# ---------------------------------------------------------------------------
# Phase 4 Vision API: read snapshot via Gemini
# ---------------------------------------------------------------------------

_SNAPSHOT_READ_PROMPT = (
    "You are reading a rendered markdown note stored as a PNG image. "
    "Summarise the note content concisely: include the title, key findings or decisions, "
    "methods or tools mentioned, any numerical results, and next actions if present. "
    "Do NOT describe the image as an image — treat the content as the note itself."
)


def read_snapshot_with_ollama(
    snapshot_path: Path,
    model: str = "qwen2.5vl:7b",
    fallback_model: str = "moondream",
    ollama_url: str = "http://localhost:11434",
) -> str | None:
    """Read a note snapshot via local Ollama vision model (REST API).

    Tries model first; falls back to fallback_model if load fails (e.g. OOM).
    qwen2.5vl:7b needs ~6 GB RAM; moondream needs ~1.1 GB.
    """
    import base64
    import json
    import urllib.request

    img_b64 = base64.b64encode(snapshot_path.read_bytes()).decode()
    prompt = (
        "This is a rendered markdown note screenshot. "
        "What is the title? List the key points, any decisions made, "
        "tools or methods mentioned, numerical results, and next actions."
    )

    for m in [model, fallback_model]:
        try:
            payload = json.dumps({
                "model": m,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
            }).encode()
            req = urllib.request.Request(
                f"{ollama_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=90)
            data = json.loads(resp.read())
            text = data.get("response", "").strip()
            if text:
                return f"[via {m}]\n{text}"
        except Exception:
            continue
    return None


def read_snapshot_with_gemini(snapshot_path: Path, vault: Path) -> str | None:
    """Pass a PNG snapshot to Gemini CLI and return the note summary.

    Uses @filepath syntax so Gemini reads the image directly (not via file tools).
    """
    try:
        # Reject DB-sourced paths that escape the vault
        resolved = Path(snapshot_path).resolve()
        if not resolved.is_relative_to(vault.resolve()):
            return None

        try:
            rel = resolved.relative_to(vault.resolve())
        except ValueError:
            rel = resolved

        prompt = f"@{rel} {_SNAPSHOT_READ_PROMPT}"
        result = subprocess.run(
            ["gemini", "--output-format", "text", "-p", prompt],
            capture_output=True, text=True, timeout=90,
            cwd=str(vault),
        )
        if result.returncode == 0 and result.stdout.strip():
            lines = [l for l in result.stdout.splitlines()
                     if not any(skip in l for skip in
                                ("YOLO mode", "Ripgrep", "MCP issues", "Warning:", "Loaded"))]
            return "\n".join(lines).strip() or None
    except Exception:
        pass
    return None


def measure_snapshot_tokens(snapshot_path: Path, vault: Path) -> int | None:
    """Ask Gemini to count how many tokens it used reading the snapshot.
    Returns token count or None if unavailable."""
    try:
        result = subprocess.run(
            ["gemini", "--output-format", "json", "-", str(snapshot_path)],
            input="How many visual tokens did you use to process this image? Reply with just a number.",
            capture_output=True, text=True, timeout=60,
            cwd=str(vault),
        )
        if result.returncode == 0 and result.stdout.strip():
            import json
            data = json.loads(result.stdout)
            # Gemini JSON output may include token usage
            usage = data.get("usageMetadata", {})
            return usage.get("totalTokenCount")
    except Exception:
        pass
    return None
