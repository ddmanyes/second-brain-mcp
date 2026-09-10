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
from . import llm_cli

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


_OCR_PROMPT_ECHO_MARKERS = (
    "analyse this scientific figure",
    "all text visible in the figure",
)


def _clean_ocr_text(value: object) -> str:
    """Drop a local VLM prompt echo without rewriting genuine figure text."""
    text = str(value or "").strip()
    lowered = text.lower()
    if any(marker in lowered for marker in _OCR_PROMPT_ECHO_MARKERS):
        return ""
    return text


# ---------------------------------------------------------------------------
# VLM analysis via Claude API
# ---------------------------------------------------------------------------

def analyse_figure(image_path: Path, caption: str = "") -> dict | None:
    """OCR + one-sentence description for one figure image, via the VLM seam.

    ``caption`` (from Phase 2 page detection) is threaded in as context so the
    OCR/description is more accurate.

    Returns:
        ``{"ocr_text", "description", "_usage"}``, or **None when the VLM gave no
        answer** (backend down, or reply with no parsable JSON). None and "the
        figure genuinely has no text" are different facts: a figure row written
        from a None result would claim we looked at the image when we did not.
    """
    caption_ctx = f"Caption: {caption}\n" if caption else ""
    prompt = (
        f"{caption_ctx}"
        "Analyse this scientific figure. Return one valid JSON object with exactly two "
        "string fields: ocr_text and description. Limit ocr_text to at most 1200 "
        "characters; prioritize titles, panel labels, axes, legends, method and gene "
        "names. Limit description to one sentence. Always close all JSON quotes and "
        "braces. No markdown."
    )
    answer = llm_cli.vision_json(prompt, image_path, expect="object")
    if answer is None:
        return None
    data = answer.data if isinstance(answer.data, dict) else {}
    return {
        "ocr_text": _clean_ocr_text(data.get("ocr_text", "")),
        "description": data.get("description", ""),
        "_usage": answer.usage,
    }


def _analysis_or_warn(image_path: Path, caption: str = "") -> dict:
    """analyse_figure with the "VLM said nothing" case handled in exactly one place.

    The cropped PNG is real and worth keeping even when the VLM is unreachable, so
    the figure is still recorded — but the failure is announced instead of being
    laundered into an empty-string row that looks like a successful analysis.
    """
    analysis = analyse_figure(image_path, caption)
    if analysis is not None:
        return analysis
    print(
        f"[figures] VLM analysis unavailable for {image_path.name} — "
        "figure saved without OCR/description",
        file=sys.stderr,
    )
    return {"ocr_text": "", "description": "", "_usage": {"input": 0, "output": 0}}


# ---------------------------------------------------------------------------
# Phase 2: PDF page-render + VLM figure detection + crop
#
# pdfimages can only pull *embedded raster* images; vector figures (matplotlib
# charts, SVG-derived diagrams) are invisible to it. Instead we render each page
# to PNG, ask a VLM for figure bounding boxes, and crop. See IMPLEMENTATION_PLAN.
# ---------------------------------------------------------------------------

_DETECT_MODEL = "claude-sonnet-4-6"


class VisionUnavailable(RuntimeError):
    """The VLM produced no usable answer for an image.

    Distinct from an empty result: "the model looked and found nothing" is a fact
    worth caching, "we never got an answer" must be retried.
    """


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


def _detect_figures_on_page(page_png: Path, page_num: int) -> tuple[list[dict], dict]:
    """Ask Claude (vision) for figures/tables on a rendered page.

    Returns a list of {"bbox": [x0,y0,x1,y1] in PIXELS, "caption": str, "type": str}.
    The VLM is asked for NORMALISED 0–1000 coordinates (VLMs calibrate relative
    coords far better than absolute pixels); we convert back to pixels here.

    Raises:
        VisionUnavailable: the VLM gave no answer. The caller must NOT mark the
            page processed on this path — an empty return means "the model looked
            and found nothing", which is cached permanently.
    """
    from PIL import Image as _PILImage

    with _PILImage.open(page_png) as im:
        w, h = im.size

    prompt = (
        f"This is page {page_num} of a scientific paper.\n"
        "Identify all figures, charts, diagrams, and tables.\n"
        "EXCLUDE the following — do NOT return bboxes for:\n"
        "  - Body text paragraphs (blocks of prose/sentences)\n"
        "  - Page headers (journal name, article type, DOI, URL lines at top)\n"
        "  - Section headings or titles (e.g. 'Introduction', 'Results')\n"
        "  - Author lists or affiliation text\n"
        "Only include regions that contain actual visual data: plots, graphs, "
        "heatmaps, diagrams, microscopy images, or data tables with rows/columns.\n"
        "The bbox must tightly wrap the visual content + its caption, "
        "but must NOT include surrounding body text or page headers.\n"
        'Return ONLY a JSON array: [{"bbox": [x0, y0, x1, y1], "caption": "...", "type": "figure|table"}]\n'
        "bbox coordinates are NORMALISED 0-1000 from the top-left corner "
        "(x0,y0 = top-left, x1,y1 = bottom-right). caption is the figure/table "
        "caption text if visible, else empty string. If there are no figures, return []."
    )
    answer = llm_cli.vision_json(
        prompt, page_png, expect="array", model=_DETECT_MODEL
    )
    if answer is None:
        raise VisionUnavailable(f"no VLM answer for page {page_num}")
    usage = answer.usage
    items = answer.data if isinstance(answer.data, list) else []
    # Header guard: academic papers have a header band in the top ~6% of the page
    # (journal name, article type, DOI/URL). If a bbox starts in that band, push
    # y0 down to just below it. This prevents the header from being cropped in.
    HEADER_GUARD_Y = 60   # normalised 0-1000; ~6% from top

    out: list[dict] = []
    for it in items:
        bbox = it.get("bbox")
        if not bbox or len(bbox) != 4:
            continue
        x0, y0, x1, y1 = bbox
        # Push top edge below header band
        y0 = max(y0, HEADER_GUARD_Y)
        # Skip if bbox collapsed or is too small after adjustment
        if y1 - y0 < 50 or x1 - x0 < 50:
            continue
        px = [
            int(x0 / 1000 * w), int(y0 / 1000 * h),
            int(x1 / 1000 * w), int(y1 / 1000 * h),
        ]
        out.append({
            "bbox": px,
            "caption": (it.get("caption") or "").strip(),
            "type": (it.get("type") or "figure").strip(),
        })
    return out, usage


# ---------------------------------------------------------------------------
# Geometry-first figure detection
#
# A VLM asked for "normalised 0-1000 bbox" answers plausibly but not precisely,
# and a plausible box is a bad crop: slivers through a panel, captions cut in
# half, body text dragged in. The PDF already carries exact coordinates for
# every image and vector path it draws, so the box is read, not guessed. Ink is
# stamped onto a coarse grid, dilated so the panels of one figure fuse into one
# blob, connected-component labelled, tightened back onto the real rects, then
# grown to swallow the "Figure N." block underneath. The VLM keeps the jobs it
# is actually good at: reading a crop, and handling pages that are pure scans.
# ---------------------------------------------------------------------------

_GEOM_CELL = 2.0        # pt per grid cell
_GEOM_GAP = 16.0        # pt — ink closer than this belongs to the same figure
_GEOM_PAD = 4.0         # pt of breathing room around the final crop
_GEOM_MIN_SIDE = 55.0   # pt — below this a blob is furniture, not a figure
_GEOM_MIN_AREA = 9000.0  # pt^2
_CAPTION_GAP = 70.0     # pt — how far a caption may sit from its figure
_CROP_DPI = 200         # crops are rendered from the page, not from a page PNG
_MAX_PAGES = 20
_DETECTOR_VERSION = "geom-2"

_CAPTION_RE = re.compile(
    r"^\s*(fig(?:ure)?\.?\s*\d|table\s*\d|extended\s+data|"
    r"supplementary\s+(?:fig|table)|scheme\s*\d)",
    re.I,
)


def _text_blocks(page) -> list:
    return [b for b in page.get_text("blocks") if len(b) <= 6 or b[6] == 0]


def _prose_rects(page) -> list:
    """Body-text blocks: several lines of long lines. Axis labels never qualify."""
    import fitz

    out = []
    for b in _text_blocks(page):
        lines = [ln for ln in (b[4] or "").splitlines() if ln.strip()]
        if len(lines) >= 2 and sum(len(ln) for ln in lines) / len(lines) >= 45:
            out.append(fitz.Rect(b[:4]))
    return out


def _sized_blocks(page) -> list[tuple]:
    """(rect, dominant font size, text) per text block, in reading order."""
    import fitz

    out = []
    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        sizes, txt = [], []
        for line in b.get("lines", []):
            for span in line.get("spans", []):
                sizes.append(round(span.get("size", 0.0), 1))
                txt.append(span.get("text", ""))
        if not sizes:
            continue
        size = max(set(sizes), key=sizes.count)
        out.append((fitz.Rect(b["bbox"]), size, "".join(txt).strip()))
    out.sort(key=lambda t: (round(t[0].y0, 1), round(t[0].x0, 1)))
    return out


_CAPTION_RUN_GAP = 14.0     # pt between a caption block and its continuation


def _caption_run(blocks: list[tuple], start: int, figure_rect) -> tuple:
    """Absorb a caption's continuation blocks.

    A long caption is split into several blocks — "(A) ... (B) ..." each land
    separately — and only the first one starts with "Figure N.". The rest are
    recognised by sitting directly underneath at the same font size, which body
    text set in a different size will not match.
    """
    import fitz

    cap_rect, cap_size, cap_text = blocks[start]
    rect = fitz.Rect(cap_rect)
    parts = [cap_text]
    prev = cap_rect
    for r, size, text in blocks[start + 1:]:
        if abs(size - cap_size) > 0.6:
            break
        if r.y0 - prev.y1 > _CAPTION_RUN_GAP or r.y1 <= prev.y0:
            break
        if r.x0 < figure_rect.x0 - 12 or r.x1 > figure_rect.x1 + 12:
            break
        if _CAPTION_RE.match(text):
            break                       # the next figure's caption
        rect |= r
        parts.append(text)
        prev = r
    return rect, " ".join(" ".join(parts).split())[:500]


def _ink_rects(page) -> list:
    """Every mark the page draws, minus page furniture (rules, header/footer bits)."""
    import fitz

    pr = page.rect
    head, foot = pr.y0 + pr.height * 0.055, pr.y1 - pr.height * 0.055
    out = []
    for img in page.get_images(full=True):
        try:
            rects = page.get_image_rects(img[0])
        except Exception:
            continue
        for r in rects:
            r = fitz.Rect(r) & pr
            # No size gate here: publishers routinely slice one figure into
            # dozens of thin strips (273x10pt seen in the wild). Clustering
            # fuses them; the component gate below is what drops real icons.
            if r.width > 4 and r.height > 4:
                out.append(r)
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"]) & pr
        if r.is_empty or r.is_infinite or r.width <= 0 or r.height <= 0:
            continue
        if r.height < 3 and r.width > pr.width * 0.5:
            continue        # full-width rule
        if r.width < 3 and r.height > pr.height * 0.5:
            continue        # full-height rule
        if r.width < 1.5 and r.height < 1.5:
            continue
        # Only small marks get banished by band — a real figure may reach the edge.
        if r.height < 20 and (r.y1 < head or r.y0 > foot):
            continue
        out.append(r)
    return out


def _cluster_rects(rects: list, pr) -> list:
    """Dilated-grid connected components, each tightened back onto its own rects."""
    import fitz

    if not rects:
        return []
    cell = _GEOM_CELL
    nx = max(1, int(pr.width / cell) + 2)
    ny = max(1, int(pr.height / cell) + 2)
    grid = bytearray(nx * ny)
    d = int(_GEOM_GAP / cell / 2)

    for r in rects:
        x0 = max(0, int((r.x0 - pr.x0) / cell) - d)
        x1 = min(nx - 1, int((r.x1 - pr.x0) / cell) + d)
        y0 = max(0, int((r.y0 - pr.y0) / cell) - d)
        y1 = min(ny - 1, int((r.y1 - pr.y0) / cell) + d)
        for y in range(y0, y1 + 1):
            row = y * nx
            grid[row + x0:row + x1 + 1] = b"\x01" * (x1 - x0 + 1)

    label = [0] * (nx * ny)
    comp = 0
    for start in range(nx * ny):
        if not grid[start] or label[start]:
            continue
        comp += 1
        stack = [start]
        label[start] = comp
        while stack:
            i = stack.pop()
            y, x = divmod(i, nx)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                yy, xx = y + dy, x + dx
                if 0 <= yy < ny and 0 <= xx < nx:
                    j = yy * nx + xx
                    if grid[j] and not label[j]:
                        label[j] = comp
                        stack.append(j)

    boxes: dict[int, object] = {}
    for r in rects:
        cx = min(nx - 1, max(0, int(((r.x0 + r.x1) / 2 - pr.x0) / cell)))
        cy = min(ny - 1, max(0, int(((r.y0 + r.y1) / 2 - pr.y0) / cell)))
        cid = label[cy * nx + cx]
        if not cid:
            continue
        boxes[cid] = (boxes[cid] | r) if cid in boxes else fitz.Rect(r)
    return list(boxes.values())


_TABLE_RE = re.compile(r"^\s*(table|supplementary\s+table|extended\s+data\s+table)\s*\d", re.I)
_TABLE_RUN_GAP = 24.0    # pt between a table's caption and its rows


def _text_table_region(blocks: list[tuple], start: int, prose: list) -> object:
    """Build a table's rect from text alone.

    Journals typeset many tables with no ruling lines at all — no drawings, no
    images, nothing for the ink clustering to find. Such a table is a "Table N"
    caption followed by a run of short-lined blocks in the same column, ending
    where real prose resumes.
    """
    import fitz

    cap_rect, cap_size, _cap_text = blocks[start]
    rect = fitz.Rect(cap_rect)
    prev = cap_rect
    for r, size, text in blocks[start + 1:]:
        if r.y0 < prev.y1 - 2:
            continue                    # sits alongside, not below — other column
        # Rows are often a hair wider than the caption, so overlap decides the
        # column, not containment.
        overlap = min(r.x1, cap_rect.x1) - max(r.x0, cap_rect.x0)
        if overlap < 0.6 * min(r.width, cap_rect.width):
            continue
        if r.y0 - prev.y1 > _TABLE_RUN_GAP:
            break                       # the table ended some blocks ago
        if abs(size - cap_size) > 1.2:
            break                       # body text is set larger than the table
        if _CAPTION_RE.match(text):
            break                       # the next figure or table
        if any((r & pr).get_area() > r.get_area() * 0.8 for pr in prose):
            break                       # body text resumed
        rect |= r
        prev = r
    return rect


def _detect_figures_geometric(page) -> list[dict]:
    """Figures on one page as {"rect": fitz.Rect (PDF points), "caption": str}."""
    import fitz

    pr = page.rect
    prose = _prose_rects(page)

    kept = []
    for r in _cluster_rects(_ink_rects(page), pr):
        if r.width < _GEOM_MIN_SIDE or r.height < _GEOM_MIN_SIDE:
            continue
        if r.get_area() < _GEOM_MIN_AREA:
            continue
        # Mostly body text underneath -> decorated prose, not a figure.
        if sum((r & p).get_area() for p in prose) > r.get_area() * 0.55:
            continue
        kept.append(r)

    blocks = _sized_blocks(page)
    cap_idx = [i for i, (_r, _s, t) in enumerate(blocks) if _CAPTION_RE.match(t)]
    used: set[int] = set()
    out: list[dict] = []
    for r in kept:
        best, best_d = None, _CAPTION_GAP
        for i in cap_idx:
            if i in used:
                continue
            cr = blocks[i][0]
            if cr.x1 < r.x0 - 12 or cr.x0 > r.x1 + 12:
                continue        # different column
            dv = cr.y0 - r.y1 if cr.y0 >= r.y1 else r.y0 - cr.y1
            if 0 <= dv < best_d:
                best, best_d = i, dv
        caption = ""
        if best is not None:
            used.add(best)
            cr, caption = _caption_run(blocks, best, r)
            r = r | cr
        rect = (fitz.Rect(r) + (-_GEOM_PAD, -_GEOM_PAD, _GEOM_PAD, _GEOM_PAD)) & pr
        out.append({"rect": rect, "caption": caption})

    for i in cap_idx:
        if i in used or not _TABLE_RE.match(blocks[i][2]):
            continue
        r = _text_table_region(blocks, i, prose)
        if r.width < _GEOM_MIN_SIDE or r.height < _GEOM_MIN_SIDE:
            continue
        if r.get_area() < _GEOM_MIN_AREA:
            continue
        rect = (fitz.Rect(r) + (-_GEOM_PAD, -_GEOM_PAD, _GEOM_PAD, _GEOM_PAD)) & pr
        out.append({
            "rect": rect,
            "caption": " ".join(blocks[i][2].split())[:500],
        })

    out.sort(key=lambda d: (round(d["rect"].y0, 1), round(d["rect"].x0, 1)))
    return out


def _page_is_scan(page) -> bool:
    """One big image and no extractable text: geometry has nothing to read here.

    A page with no figures at all is NOT a scan — it is just a page with no
    figures, and sending it to the VLM would buy nothing.
    """
    import fitz

    if len(page.get_text("text").strip()) >= 200:
        return False
    area = page.rect.get_area()
    for img in page.get_images(full=True):
        try:
            rects = page.get_image_rects(img[0])
        except Exception:
            continue
        if any(fitz.Rect(r).get_area() > area * 0.8 for r in rects):
            return True
    return False


def _page_digest(page) -> str:
    """Stable cache key over the page's text + geometry, versioned by detector."""
    parts = [_DETECTOR_VERSION, page.get_text("text")]
    parts += [f"{r.x0:.1f},{r.y0:.1f},{r.x1:.1f},{r.y1:.1f}" for r in _ink_rects(page)]
    return hashlib.md5(
        "|".join(parts).encode("utf-8", "replace"), usedforsecurity=False
    ).hexdigest()[:16]


def _crop_page_region(page, rect, dest: Path) -> bool:
    """Render one region straight off the PDF page. Sharper than cropping a page PNG."""
    import fitz

    if rect.width < 20 or rect.height < 20:
        return False
    try:
        pix = page.get_pixmap(
            matrix=fitz.Matrix(_CROP_DPI / 72, _CROP_DPI / 72),
            clip=rect,
            colorspace=fitz.csRGB,
        )
    except Exception as e:
        print(f"[figures] crop render failed: {e}", file=sys.stderr)
        return False
    if pix.width < 50 or pix.height < 50:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    pix.save(str(dest))
    return True


def _reset_if_detector_changed(note_path: str, fig_dir: Path) -> None:
    """Wipe crops made by an older detector instead of appending beside them."""
    stamp = fig_dir / ".detector-version"
    try:
        current = stamp.read_text(encoding="utf-8").strip()
    except OSError:
        current = ""
    if current == _DETECTOR_VERSION:
        return
    for old in fig_dir.glob("fig-*"):
        try:
            old.unlink()
        except OSError:
            pass
    thumb_dir = FIGURES_DIR.parent / ".figure-thumbs" / fig_dir.name
    for old in thumb_dir.glob("fig-*"):
        try:
            old.unlink()
        except OSError:
            pass
    try:
        vault_db.clear_figures_for_note(note_path)
    except Exception as e:
        print(f"[figures] could not clear old figure rows: {e}", file=sys.stderr)
    fig_dir.mkdir(parents=True, exist_ok=True)
    stamp.write_text(_DETECTOR_VERSION, encoding="utf-8")


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


def make_figure_thumbnail(
    src: Path, note_path: str, fig_index: int, max_edge: int = 768
) -> Path | None:
    """Down-scale a figure to a thumbnail (long edge ≤ max_edge) for cheap recall.

    Stored in a HIDDEN dir (`.figure-thumbs/`) so Obsidian/glob don't treat the
    thumbnail as an extra figure. Returns the thumbnail path, or None on failure.
    """
    try:
        from PIL import Image as _PILImage
        if not src.exists():
            return None
        out_dir = FIGURES_DIR.parent / ".figure-thumbs" / _figure_slug(note_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        dest = out_dir / f"fig-{fig_index:02d}.png"
        # Cache on (note, index) BUT never serve a thumbnail older than its
        # source: re-extraction rewrites fig-NN.png in place, and a stale
        # thumbnail would keep showing the previous crop to every reader.
        if dest.exists() and dest.stat().st_mtime >= src.stat().st_mtime:
            return dest
        with _PILImage.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((max_edge, max_edge))
            im.save(str(dest), "PNG")
        return dest
    except Exception as e:
        print(f"[figures] thumbnail failed for {src}: {e}", file=sys.stderr)
        return None


def _estimate_image_tokens(image_path: Path, cap: int = 2576) -> int:
    """Rough Anthropic-vision token estimate from pixel dims (~1 token per 28x28 patch).

    The vision API downsamples any image whose long edge exceeds ``cap`` pixels before
    tokenizing it, so the cap must be applied to the dimensions *first* — estimating
    straight off the raw pixel size overestimates a tall/narrow image by ~3x (a 1280x4455
    full-page snapshot: 7,272 tokens off raw pixels vs. ~2,430 once resized to a 2576px
    long edge, which is what the API actually charges for). See
    快照-png-的-token-經濟實測-壓縮後讀取反而更貴 (2026-08-18).
    """
    try:
        from PIL import Image as _PILImage
        with _PILImage.open(image_path) as im:
            w, h = im.size
        if max(w, h) > cap:
            k = cap / max(w, h)
            w, h = int(w * k), int(h * k)
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
    fig_slug = _figure_slug(note_path)
    new_section = "\n\n## Extracted Figures\n" + "".join(
        f"![[figures/{fig_slug}/{fig}]]\n" for fig in figs
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
            analysis = _analysis_or_warn(local)
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
    """Primary path: bboxes read from the PDF's own geometry, cropped off the page.

    Pages that carry no extractable geometry (pure scans) still go through the
    VLM bbox route, since there is nothing else to read there. Every crop is
    described by the VLM regardless — that part was never the weak link.

    Returns a list of figure dicts on success (possibly empty), or None to signal
    "nothing usable here" so the caller falls back to pdfimages.
    """
    import fitz
    import shutil

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        print(f"[figures] cannot open PDF: {e}", file=sys.stderr)
        return None

    _reset_if_detector_changed(note_path, fig_dir)

    fig_index = len(list(fig_dir.glob("fig-*.png")))
    results: list[dict] = []
    # Sonnet 4.6: $3/$15 per M input/output; Haiku 4.5: $0.80/$4 per M
    _SONNET_IN, _SONNET_OUT = 3.0, 15.0
    _HAIKU_IN, _HAIKU_OUT = 0.80, 4.0
    detect_tok = {"input": 0, "output": 0}
    analyse_tok = {"input": 0, "output": 0}
    scan_pages: list[int] = []

    def _record(dest: Path, caption: str) -> None:
        nonlocal fig_index
        analysis = _analysis_or_warn(dest, caption)
        a_usage = analysis.pop("_usage", {})
        analyse_tok["input"] += a_usage.get("input", 0)
        analyse_tok["output"] += a_usage.get("output", 0)
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

    try:
        for pnum, page in enumerate(doc):
            if pnum >= _MAX_PAGES:
                break
            try:
                digest = _page_digest(page)
                if vault_db.page_is_processed(note_path, digest):
                    continue
                dets = _detect_figures_geometric(page)
            except Exception as e:
                print(f"[figures] geometry failed on page {pnum}: {e}", file=sys.stderr)
                continue
            if _page_is_scan(page):
                scan_pages.append(pnum)   # unmarked on purpose — the VLM pass owns it
                continue
            for det in dets:
                dest = fig_dir / f"fig-{fig_index:02d}.png"
                if not _crop_page_region(page, det["rect"], dest):
                    continue
                _record(dest, det.get("caption", ""))
            vault_db.mark_page_processed(note_path, digest, len(dets))
    finally:
        doc.close()

    # Scans: no geometry to read, so fall back to asking the VLM where things are.
    scan_ok = scan_err = False
    if scan_pages:
        page_pngs = []
        try:
            page_pngs = _render_pdf_pages(str(pdf_path), max_pages=_MAX_PAGES)
        except Exception as e:
            print(f"[figures] page render failed: {e}", file=sys.stderr)
            scan_err = True
        for pnum in scan_pages:
            if pnum >= len(page_pngs):
                continue
            page_png = page_pngs[pnum]
            page_hash = hashlib.md5(
                page_png.read_bytes(), usedforsecurity=False
            ).hexdigest()[:16]
            if vault_db.page_is_processed(note_path, page_hash):
                continue
            try:
                dets, det_usage = _detect_figures_on_page(page_png, pnum)
                detect_tok["input"] += det_usage.get("input", 0)
                detect_tok["output"] += det_usage.get("output", 0)
            except Exception as e:
                print(f"[figures] VLM detect failed on page {pnum}: {e}", file=sys.stderr)
                scan_err = True
                continue        # transient — don't mark, so a later run retries
            scan_ok = True
            for det in dets:
                dest = fig_dir / f"fig-{fig_index:02d}.png"
                if not _crop_figure(page_png, det["bbox"], dest):
                    continue
                _record(dest, det.get("caption", ""))
            vault_db.mark_page_processed(note_path, page_hash, len(dets))
        if page_pngs:
            shutil.rmtree(page_pngs[0].parent, ignore_errors=True)

    if detect_tok["input"] or analyse_tok["input"]:
        detect_cost = (detect_tok["input"] * _SONNET_IN + detect_tok["output"] * _SONNET_OUT) / 1_000_000
        analyse_cost = (analyse_tok["input"] * _HAIKU_IN + analyse_tok["output"] * _HAIKU_OUT) / 1_000_000
        total_cost = detect_cost + analyse_cost
        print(
            f"[figures] extraction cost: detect={detect_tok['input']}in/{detect_tok['output']}out "
            f"(${detect_cost:.4f}) analyse={analyse_tok['input']}in/{analyse_tok['output']}out "
            f"(${analyse_cost:.4f}) total=${total_cost:.4f}",
            file=sys.stderr,
        )

    # Nothing found and the only route left (the VLM) never answered → let the
    # caller try pdfimages.
    if not results and scan_err and not scan_ok:
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

        analysis = _analysis_or_warn(local)

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
        vault_db.update_snapshot(note_path, str(out_path), tier, _estimate_image_tokens(out_path))
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
    except Exception as exc:
        print(
            f"[figures] snapshot rendering failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return None
    finally:
        tmp_html.unlink(missing_ok=True)

    if out_path.exists():
        vault_db.update_snapshot(note_path, str(out_path), tier, _estimate_image_tokens(out_path))
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
        "token_est": _estimate_image_tokens(out),
        "size_kb": size_kb,
    }
