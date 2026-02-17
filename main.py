# main.py — Clean, commented, no venv, Qt-only helpers, with progress repaint & opacity sliders

import os
import re
import random
import traceback
import time
from typing import List, Dict, Optional

from aqt import mw
from aqt.utils import showWarning
from aqt.qt import (
    QAction, QFileDialog, QInputDialog, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox, QRadioButton, QSpinBox, QPushButton, QButtonGroup,
    QColorDialog
)

from aqt.qt import (
    QAction, QFileDialog, QInputDialog, QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox, QRadioButton, QSpinBox, QPushButton, QButtonGroup,
    QColorDialog, QDialogButtonBox, QScrollArea, QWidget
)

from .pdf_parser import (
    extract_words_with_boxes,
    extract_image_boxes,
    extract_text_from_pdf,
    semantic_sentence_rects,   # <-- ADD THIS
)

# Colorizer entry points (used from the Options dialog buttons)
from .colorizer import open_coloration_settings_dialog, on_edit_color_table

# Text/rect extraction and OCR-first page text
from .pdf_parser import (
    extract_words_with_boxes, boxes_for_phrase, sentence_rects_for_phrase,
    extract_image_boxes, extract_text_from_pdf
)

# PNG rendering (plain and with highlights)
from .pdf_images import render_page_as_png, render_page_as_png_with_highlights

# OpenAI-backed card/output helpers
from .openai_cards import suggest_occlusions_from_image, generate_cards


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

_OCCLUSION_DPI = 200
_IMAGE_MARGIN_PDF_PT = 36.0  # ~0.5″ margin around detected images
_MAX_MASKS_PER_CROP = 12
ADDON_ID = os.path.basename(os.path.dirname(__file__))


import math

def _cosine(a, b):
    return sum(x*y for x,y in zip(a,b)) / (
        math.sqrt(sum(x*x for x in a)) * math.sqrt(sum(y*y for y in b)) + 1e-9
    )

# ──────────────────────────────────────────────────────────────────────────────
# Debug logger
# ──────────────────────────────────────────────────────────────────────────────

def _dbg(msg: str) -> None:
    """Append timestamped debug line to profile log."""
    try:
        path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

def _is_real_cloze(text: str) -> bool:
    import re
    return bool(re.search(r"\{\{c\d+::.+?\}\}", text or ""))

import re

_CLOZE_RE = re.compile(r"\{\{c(\d+)::(.*?)(?:(::)(.*?))?\}\}", re.DOTALL)

def _style_from_colorizer_flags(color_hex: str, bold: bool, italic: bool) -> str:
    """Build a CSS style string for the cloze wrapper."""
    parts = [f"color:{color_hex};"]
    if bold:
        parts.append("font-weight:bold;")
    if italic:
        parts.append("font-style:italic;")
    return "".join(parts)

def _wrap_all_clozes_with_style(text: str, style_str: str) -> str:
    """Wrap all cloze answers in a <span style="...">...</span>, preserving hints."""
    if not text or not isinstance(text, str) or not style_str:
        return text

    def _one(m: re.Match) -> str:
        num = m.group(1)
        ans = m.group(2) or ""
        has_hint = bool(m.group(3))
        hint = m.group(4) or ""
        # If already contains a span with color, keep it (avoid double wrap)
        if "<span" in ans and "color:" in ans:
            body = ans
        else:
            body = f'<span style="{style_str}">{ans}</span>'
        if has_hint:
            return f"{{{{c{num}::{body}::{hint}}}}}"
        else:
            return f"{{{{c{num}::{body}}}}}"

    return _CLOZE_RE.sub(_one, text)

def _colors_from_color_table_safe() -> list[str]:
    """
    Extract a deduped list of usable CSS colors from the colorizer table.
    Accept #RRGGBB, #RGB, rgb()/rgba(), hsl()/hsla(), and CSS named colors.
    Also understands common keys like 'color', 'colour', 'hex', 'fg', 'css'.
    Falls back to a pleasant light palette if the table is empty.
    """
    def _is_css_color(s: str) -> bool:
        if not isinstance(s, str):
            return False
        s = s.strip()
        if not s:
            return False
        if s.startswith("#"):
            # accept #RGB, #RRGGBB, #RRGGBBAA, etc. (we’ll pass it through)
            return True
        low = s.lower()
        return (
            low.startswith("rgb(") or low.startswith("rgba(") or
            low.startswith("hsl(") or low.startswith("hsla(") or
            # crude allow-list for named colors; allow any a–z string
            (low[0].isalpha() and all(ch.isalpha() for ch in low.replace(" ", "")))
        )

    colors: list[str] = []
    seen = set()
    try:
        from .colorizer import get_color_table
        tbl = get_color_table() or []
    except Exception:
        tbl = []

    # Accept multiple shapes: dict rows, (pattern, color), or custom
    for row in tbl:
        cand = None
        if isinstance(row, dict):
            # Try common keys in order
            for k in ("color", "colour", "hex", "fg", "css"):
                v = row.get(k)
                if isinstance(v, str) and _is_css_color(v):
                    cand = v.strip()
                    break
        elif isinstance(row, (list, tuple)) and len(row) >= 2 and isinstance(row[1], str):
            v = row[1]
            if _is_css_color(v):
                cand = v.strip()

        if cand and cand not in seen:
            seen.add(cand)
            colors.append(cand)

    # Fallback palette (light tints that read well on dark backgrounds)
    if not colors:
        colors = [
            "#8ad3ff",  # light sky
            "#ffd280",  # light orange
            "#a7ffb5",  # light green
            "#ffb3c7",  # light pink
            "#cdb3ff",  # light violet
            "#ffe680",  # light yellow
        ]

    # Debug: log chosen palette once
    try:
        _dbg(f"Random cloze color pool (n={len(colors)}): {colors}")
    except Exception:
        pass

    return colors


def _wrap_one_cloze_answer(m: re.Match, color_hex: str) -> str:
    """Rebuild a single cloze with the answer wrapped in a span color, preserving hint."""
    num = m.group(1)
    ans = m.group(2) or ""
    has_hint = bool(m.group(3))
    hint = m.group(4) or ""
    # Avoid double-wrapping if answer already contains a span with a color.
    if "<span" in ans and "color:" in ans:
        body = ans
    else:
        body = f'<span style="color:{color_hex};">{ans}</span>'
    if has_hint:
        return f"{{{{c{num}::{body}::{hint}}}}}"
    else:
        return f"{{{{c{num}::{body}}}}}"

def _wrap_all_clozes_with_color(text: str, color_hex: str) -> str:
    """Wrap all cloze answers in the given text with a single color."""
    if not text or not isinstance(text, str):
        return text
    if not (isinstance(color_hex, str) and color_hex.startswith("#")):
        return text
    return _CLOZE_RE.sub(lambda m: _wrap_one_cloze_answer(m, color_hex), text)


# ──────────────────────────────────────────────────────────────────────────────
# Config I/O
# ──────────────────────────────────────────────────────────────────────────────

def _get_config() -> dict:
    """Read add-on config with defaults (persisted across runs)."""
    c = mw.addonManager.getConfig(ADDON_ID) or {}
    c.setdefault("highlight_enabled", True)
    c.setdefault("highlight_color_hex", "#FF69B4")
    c.setdefault("highlight_fill_alpha", 140)      # NEW: persisted fill opacity (0–255)
    c.setdefault("highlight_outline_alpha", 230)   # NEW: persisted outline opacity (0–255)
    c.setdefault("occlusion_enabled", True)
    c.setdefault("openai_api_key", "")
    c.setdefault("types_basic", True)
    c.setdefault("types_cloze", False)
    c.setdefault("per_slide_mode", "ai")
    c.setdefault("per_slide_min", 1)
    c.setdefault("per_slide_max", 3)
    c.setdefault("color_after_generation", True)
    c.setdefault("ai_extend_color_table", True)
    c.setdefault("page_mode", "all")  # “all” or “range” (numeric range resets per PDF)
    c.setdefault("cloze_color_mode", "per_word")      # per_word | random_table | custom
    c.setdefault("cloze_custom_color_hex", "#FF69B4") # persisted like highlight color

    return c

def _save_config(c: dict) -> None:
    mw.addonManager.writeConfig(ADDON_ID, c)


# ──────────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────────

def _pdf_page_count(pdf_path: str) -> int:
    """Return page count via QtPdf (parent=None for PyQt6 6.6.x) or pypdf fallback."""
    # QtPdf (preferred)
    try:
        from PyQt6.QtPdf import QPdfDocument
        qdoc = QPdfDocument(None)
        qdoc.load(pdf_path)
        if qdoc.status() == QPdfDocument.Status.Ready:
            return int(qdoc.pageCount())
    except Exception:
        pass
    # pypdf (pure-Python fallback)
    try:
        from pypdf import PdfReader
        return len(PdfReader(pdf_path).pages)
    except Exception:
        return 0

def _rgba_from_hex(hex_str: str, alpha: int = 55):
    """Parse #RRGGBB into (r,g,b,a); alpha in 0..255."""
    s = (hex_str or "").strip()
    if not s.startswith("#") or len(s) != 7:
        s = "#FF69B4"
    r = int(s[1:3], 16); g = int(s[3:5], 16); b = int(s[5:7], 16)
    return (r, g, b, int(alpha))

def deck_name_from_pdf_path(pdf_path: str) -> str:
    return os.path.splitext(os.path.basename(pdf_path))[0].strip()

def get_or_create_deck(deck_name: str) -> int:
    col = mw.col
    did = col.decks.id(deck_name, create=False)
    return did or col.decks.id(deck_name, create=True)

def _write_media_file(basename: str, data: bytes) -> Optional[str]:
    """Store bytes in Anki media. Return stored filename or None."""
    try:
        return mw.col.media.write_data(basename, data)
    except Exception:
        try:
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), basename)
            with open(tmp, "wb") as f:
                f.write(data)
            return mw.col.media.add_file(tmp)
        except Exception:
            return None

def _crop_png_region(png_bytes: bytes, rect_pt: dict, dpi: int) -> bytes:
    """Crop PNG using Qt only (rect given in PDF points)."""
    if not png_bytes:
        return b""
    from PyQt6.QtGui import QImage
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
    img = QImage.fromData(png_bytes)
    if img.isNull():
        return b""
    scale = dpi / 72.0
    x = int(max(0, rect_pt.get("x", 0) * scale))
    y = int(max(0, rect_pt.get("y", 0) * scale))
    w = int(max(1, rect_pt.get("w", 0) * scale))
    h = int(max(1, rect_pt.get("h", 0) * scale))
    if x + w > img.width():  w = img.width() - x
    if y + h > img.height(): h = img.height() - y
    if w <= 0 or h <= 0:     return b""
    cropped = img.copy(x, y, w, h)
    ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.OpenModeFlag.WriteOnly)
    cropped.save(buf, b"PNG"); buf.close()
    return bytes(ba)

def _mask_one_rect_on_png(png_bytes: bytes, rect_px: dict,
                          fill=(242, 242, 242), outline=(160,160,160)) -> bytes:
    """Draw a mask rectangle onto a PNG (pixel coords), Qt-only."""
    if not png_bytes:
        return b""
    from PyQt6.QtGui import QImage, QPainter, QColor, QPen, QBrush
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QRect
    img = QImage.fromData(png_bytes)
    if img.isNull():
        return png_bytes
    p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QBrush(QColor(*fill)))
    pen = QPen(QColor(*outline)); pen.setWidth(2); p.setPen(pen)
    x = int(rect_px.get("x", 0)); y = int(rect_px.get("y", 0))
    w = int(rect_px.get("w", 0)); h = int(rect_px.get("h", 0))
    if w > 0 and h > 0:
        p.drawRect(QRect(x, y, w, h))
    p.end()
    ba = QByteArray(); buf = QBuffer(ba); buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, b"PNG"); buf.close()
    return bytes(ba)

def force_move_cards_to_deck(cids: list, deck_id: int):
    """Move cards to target deck; tolerate API differences."""
    if not cids:
        return
    col = mw.col
    try:
        col.decks.set_card_deck(cids, deck_id)
    except Exception:
        try:
            col.decks.setDeck(cids, deck_id)
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Models (Basic + Slide / Cloze + Slide) — enforced fields, templates, CSS
# ──────────────────────────────────────────────────────────────────────────────

def ensure_basic_with_slideimage(model_name: str = "Basic + Slide") -> dict:
    """Ensure a Basic model with SlideImage field and responsive CSS."""
    col = mw.col
    m = col.models.byName(model_name)
    created = False
    if not m:
        m = col.models.new(model_name)
        created = True

    # Fields
    want = ["Front", "Back", "SlideImage"]
    have = [f.get("name") for f in (m.get("flds") or [])]
    for name in want:
        if name not in have:
            col.models.addField(m, col.models.newField(name))

    # Templates
    qfmt = "{{Front}}"
    afmt = "{{FrontSide}}\n\n<hr>\n{{Back}}\n\n<hr>\n{{SlideImage}}"
    tmpls = m.get("tmpls") or []
    if not tmpls:
        t = col.models.newTemplate("Card 1")
        t["qfmt"] = qfmt; t["afmt"] = afmt
        col.models.addTemplate(m, t)
    else:
        t = tmpls[0]; t["qfmt"] = qfmt; t["afmt"] = afmt
        m["tmpls"][0] = t

    # Responsive CSS
    m["css"] = (
        ".card { font-family: arial; font-size: 20px; text-align: center; "
        "color: black; background-color: white; overflow:auto !important; }\n"
        ".card img { max-width: 96vw !important; width: auto !important; "
        "height: auto; image-rendering: crisp-edges; }\n"
    )

    if created: col.models.add(m)
    else:       col.models.save(m)
    return col.models.byName(model_name)

def get_basic_model_fallback():
    """Find a 2+ field / 1+ template model if 'Basic' is missing."""
    col = mw.col
    m = col.models.byName("Basic")
    if m:
        return m
    for cand in col.models.all():
        if len(cand.get("flds", [])) >= 2 and len(cand.get("tmpls", [])) >= 1:
            return cand
    return col.models.current()

def ensure_cloze_with_slideimage(model_name: str = "Cloze + Slide") -> dict:
    """Ensure a Cloze model with SlideImage and responsive CSS."""
    col = mw.col
    m = col.models.byName(model_name)

    def enforce(model):
        # Fields
        want = ["Text", "Back Extra", "SlideImage"]
        have = [f["name"] for f in model.get("flds", [])]
        for name in want:
            if name not in have:
                col.models.addField(model, col.models.newField(name))
        # Templates
        tmpls = model.get("tmpls") or []
        if not tmpls:
            tmpls.append(col.models.newTemplate("Cloze"))
        t = tmpls[0]
        t["qfmt"] = "{{cloze:Text}}"
        t["afmt"] = "{{cloze:Text}}\n\n{{#Back Extra}}{{Back Extra}}{{/Back Extra}}\n\n<hr>\n{{SlideImage}}"
        model["tmpls"][0] = t
        # CSS
        model["css"] = (
            ".card { font-family: arial; font-size: 20px; text-align: center; "
            "color: black; background-color: white; overflow:auto !important; }\n"
            ".card img { max-width: 96vw !important; width: auto !important; "
            "height: auto; image-rendering: crisp-edges; }\n"
        )
        col.models.save(model); return model

    if not m:
        m = col.models.new(model_name); m["type"] = 1
        col.models.add(m)
    return enforce(m)


# ──────────────────────────────────────────────────────────────────────────────
# Worker — PDF → OCR text → AI cards (+ optional occlusions) → per-card highlights
# ──────────────────────────────────────────────────────────────────────────────

def _worker_generate_cards(pdf_path: str, api_key: str, opts: dict) -> Dict:
    _dbg(f"WORKER START: pdf={pdf_path}, opts={opts}")

    def ui_update(label: str):
        try: mw.progress.update(label=label)
        except Exception: pass

    try:
        # ----- 1) Gather OCR text pages -----
        page_mode = opts.get("page_mode", "all")
        if page_mode == "range":
            page_from = max(1, int(opts.get("page_from", 1)))
            page_to   = int(opts.get("page_to", 10**9))
            if page_to < page_from:
                page_from, page_to = page_to, page_from
            max_pages = page_to - page_from + 1
            pages = extract_text_from_pdf(pdf_path, api_key, page_start=page_from, max_pages=max_pages)
        else:
            _dbg("Calling extract_text_from_pdf()...")
            pages = extract_text_from_pdf(pdf_path, api_key)

        _dbg(f"extract_text_from_pdf returned {len(pages)} pages")
        if not pages:
            return {"ok": True, "cards": [], "pages": 0, "errors": [], "meta": {"pdf_path": pdf_path}}

        total_pages = len(pages)
        results: List[dict] = []
        page_errors: List[str] = []

        # ----- 2) For each page, generate content cards -----
        mode = opts.get("per_slide_mode", "ai")
        minv = int(opts.get("per_slide_min", 1))
        maxv = int(opts.get("per_slide_max", 3))
        if maxv < minv: minv, maxv = maxv, minv

        for idx, page in enumerate(pages, start=1):
            mw.taskman.run_on_main(lambda i=idx, t=total_pages: ui_update(f"Processing page {i} of {t}"))

            text = (page.get("text") or "").strip()
            if not text:
                _dbg(f"No OCR text on page {page.get('page')} — skipping")
                continue

            _dbg(f"Generating cards for page {page['page']}: {len(text)} chars")
            try:
                cards = []
                need_basic = opts.get("types_basic") or opts.get("types_cloze")
                if need_basic:
                    _dbg(f"Calling OpenAI for BASIC cards on page {page['page']}")
                    out_basic = generate_cards(text, api_key, mode="basic")
                    cards += out_basic.get("cards", [])
                if opts.get("types_cloze"):
                    out_cloze = generate_cards(text, api_key, mode="cloze")
                    cards += out_cloze.get("cards", [])
            except Exception as e:
                page_errors.append(f"page {page['page']}: {e}")
                continue

            # Trim if “range” mode per slide
            if mode == "range" and cards:
                n = max(0, min(random.randint(minv, maxv), len(cards)))
                cloze_first, non_cloze = [], []
                for c in cards:
                    f = (c.get("front") or ""); b = (c.get("back") or "")
                    (cloze_first if "{{c" in (f+b) else non_cloze).append(c)
                cards = (cloze_first + non_cloze)[:n]

            # ----- 3) (Optional) auto-occlusion near images -----
            try:
                if bool(opts.get("occlusion_enabled", True)):
                    img_boxes = extract_image_boxes(pdf_path, page["page"])
                    occl_cards = []
                    page_png = render_page_as_png(pdf_path, page["page"], dpi=_OCCLUSION_DPI, max_width=4000) or b""
                    for r_idx, ib in enumerate(img_boxes, start=1):
                        rect_pt = {
                            "x": max(0.0, ib["x"] - _IMAGE_MARGIN_PDF_PT),
                            "y": max(0.0, ib["y"] - _IMAGE_MARGIN_PDF_PT),
                            "w": ib["w"] + 2.0 * _IMAGE_MARGIN_PDF_PT,
                            "h": ib["h"] + 2.0 * _IMAGE_MARGIN_PDF_PT,
                        }
                        crop_png = _crop_png_region(page_png, rect_pt, dpi=_OCCLUSION_DPI)
                        if not crop_png:
                            continue
                        out = suggest_occlusions_from_image(crop_png, api_key, max_masks=_MAX_MASKS_PER_CROP, temperature=0.0)
                        masks_px = (out.get("masks") if isinstance(out, dict) else []) or []
                        for i, m in enumerate(masks_px, start=1):
                            masked_png = _mask_one_rect_on_png(crop_png, m)
                            occl_cards.append({
                                "front": "", "back": "", "page": page["page"], "hi": [],
                                "_occl_assets": {
                                    "base_crop_bytes": crop_png, "masked_bytes": masked_png,
                                    "base_name":  f"occl_p{page['page']}_r{r_idx}_base.png",
                                    "masked_name":f"occl_p{page['page']}_r{r_idx}_m{i}.png",
                                },
                                "_occl_tag": "pdf2cards:ai_occlusion"
                            })
                    cards += occl_cards
            except Exception as e:
                _dbg("Auto-occlusion error: " + repr(e))

            # ----- 4) Compute highlight rects per produced card -----
            try:
                page_words = extract_words_with_boxes(pdf_path, page["page"])
            except Exception:
                page_words = []

            for card in cards:
                if card.get("_occl_assets"):
                    results.append({
                        "front": card.get("front",""), "back": card.get("back",""),
                        "page": page["page"], "hi": [],
                        "_occl_assets": card["_occl_assets"], "_occl_tag": card.get("_occl_tag")
                    })
                    continue

                
                front = (card.get("front") or "").strip()
                back  = (card.get("back")  or "").strip()

                # Determine highlight text based on card type
                is_cloze = _is_real_cloze(front) or _is_real_cloze(back)

                if is_cloze:
                    rect_text = front      # Cloze: highlight based on front-side cloze text
                else:
                    rect_text = back       # Basic: highlight based on back (actual answer)

                # Compute highlight rects
                hi_rects = []
                if opts.get("highlight_enabled", True):
                    try:
                        hi_rects = semantic_sentence_rects(
                            page_words,
                            rect_text,   # <-- the correct source text depending on card type
                            api_key,
                            max_sentences=1
                        )
                    except Exception as e:
                        _dbg(f"Semantic highlight error: {e}")
                        hi_rects = []

                results.append({ "front": front, "back": back, "page": page["page"], "hi": hi_rects })

        return {"ok": True, "cards": results, "pages": total_pages,
                "errors": page_errors, "meta": {"pdf_path": pdf_path}}

    except Exception as e:
        tb = traceback.format_exc()
        return {"ok": False, "cards": [], "pages": 0, "errors": [],
                "error": str(e), "traceback": tb}


# ──────────────────────────────────────────────────────────────────────────────
# After worker completes — insert notes + render images (+ optional color new)
# ──────────────────────────────────────────────────────────────────────────────

def _on_worker_done(result: Dict, deck_id: int, deck_name: str,
                    models: Dict[str, dict], opts: dict):

    # Errors from worker
    if not isinstance(result, dict) or not result.get("ok", False):
        tb = result.get("traceback", "") if isinstance(result, dict) else ""
        tb_snip = tb[:1200] + "\n…(truncated)…" if tb and len(tb) > 1200 else tb
        mw.progress.finish()
        showWarning(f"Generation failed.\n\nError: {result.get('error')}\n\n{tb_snip}")
        return

    cards = result.get("cards", []) or []
    pdf_path = result.get("meta", {}).get("pdf_path")
    _dbg(f"Worker produced {len(cards)} cards total")
    if not cards:
        mw.progress.finish()
        showWarning("No cards were generated.\n\n"
                    "Possible reasons:\n"
                    "• OCR returned empty text (see pdf2cards_debug.log)\n"
                    "• OpenAI returned no cards\n"
                    "Try a smaller test or a page range.\n")
        return

    want_basic = bool(opts.get("types_basic", True))
    want_cloze = bool(opts.get("types_cloze", False))
    if not (want_basic or want_cloze):
        mw.progress.finish()
        showWarning("No card types selected. Aborting.")
        return

    # Enforce models
    if want_basic:
        models["basic"] = ensure_basic_with_slideimage(models.get("basic", {}).get("name", "Basic + Slide"))
    if want_cloze:
        models["cloze"] = ensure_cloze_with_slideimage(models.get("cloze", {}).get("name", "Cloze + Slide"))

    mw.progress.start(label=f"Inserting {len(cards)} card(s)…", immediate=True)

    # ---- background: render slide+insert notes, return new note IDs ----
    def _insert_and_render() -> list:
        new_note_ids: list = []
        try:
            total = len(cards)

            

            for idx, card in enumerate(cards, start=1):
                mw.taskman.run_on_main(lambda i=idx, t=total:
                    mw.progress.update(label=f"Rendering cards… ({i}/{t})"))

                front = card.get("front","") or ""
                back  = card.get("back","")  or ""
                page_no = card.get("page")
                hi_rects = card.get("hi", []) or []
                fname = ""
                occl_tag = None

                # Occlusion assets → two-image card
                assets = card.get("_occl_assets")
                if assets:
                    base_path   = _write_media_file(assets.get("base_name","occl_base.png"),   assets.get("base_crop_bytes") or b"")
                    masked_path = _write_media_file(assets.get("masked_name","occl_masked.png"), assets.get("masked_bytes") or b"")
                    if not (base_path and masked_path):
                        _dbg("Occlusion: failed to store media; skipping card.")
                        continue
                    base_fn   = os.path.basename(base_path)
                    masked_fn = os.path.basename(masked_path)
                    front = f'<img src="{masked_fn}">'
                    back  = f'<img src="{base_fn}">'
                    occl_tag = card.get("_occl_tag")

                # Slide image (with optional highlights)
                if pdf_path and page_no:
                    try:
                        if opts.get("highlight_enabled", True) and not assets:
                            # Use persisted opacity sliders (0..255), with safe defaults
                            fill_alpha    = int(opts.get("highlight_fill_alpha", 140))
                            outline_alpha = int(opts.get("highlight_outline_alpha", 230))
                            fill_rgba     = _rgba_from_hex(opts.get("highlight_color_hex", "#FF69B4"), alpha=fill_alpha)
                            outline_rgba  = _rgba_from_hex(opts.get("highlight_color_hex", "#FF69B4"), alpha=outline_alpha)
                            _dbg(f"HIs: page={page_no} rects={len(hi_rects)}")
                            png = render_page_as_png_with_highlights(
                                pdf_path, page_no, hi_rects,
                                dpi=300, max_width=4000,
                                fill_rgba=fill_rgba, outline_rgba=outline_rgba,
                                outline_width=2
                            )
                        else:
                            png = render_page_as_png(pdf_path, page_no, dpi=300, max_width=4000)

                        if png and isinstance(png, (bytes, bytearray)) and len(png) > 0:
                            safe_deck = re.sub(r"[^A-Za-z0-9_-]+", "_", deck_name)
                            base_name = os.path.splitext(os.path.basename(pdf_path))[0]
                            suggested = f"{safe_deck}_{base_name}_p{page_no}_c{idx}.png"
                            stored = _write_media_file(suggested, png)
                            if stored:
                                fname = os.path.basename(stored)
                                _dbg(f"Stored slide image: {fname}")
                        else:
                            _dbg("Slide image bytes empty or invalid — skipping attachment.")
                    except Exception as e:
                        _dbg(f"Image render failed: {e}")

                # Insert note
                col = mw.col
                raw_front = front.strip()
                raw_back  = back.strip()
                is_cloze  = _is_real_cloze(raw_front) or _is_real_cloze(raw_back)

                # Escape braces for Basic only (avoid template tidy edge-cases)
                if not is_cloze:
                    raw_front = raw_front.replace("{", "&#123;").replace("}", "&#125;")
                    raw_back  = raw_back.replace("{", "&#123;").replace("}", "&#125;")

                if is_cloze and want_cloze:
                    try:
                        # --- Cloze coloring: decide and apply before insertion ---
                        mode = str(opts.get("cloze_color_mode", "per_word"))
                        colored_front = raw_front
                        if mode in ("random_table", "custom"):
                            # Pick the single color
                            if mode == "custom":
                                color_hex = str(opts.get("cloze_custom_color_hex") or "#FF69B4")
                            else:
                                colors = _colors_from_color_table_safe()
                                color_hex = random.choice(colors) if colors else str(opts.get("highlight_color_hex", "#FF69B4"))

                            # Read colorizer style flags (bold/italic) so we can include them
                            try:
                                from .colorizer import _read_cfg as _cc_read_cfg
                                cc = _cc_read_cfg() or {}
                                bold_on = bool(cc.get("bold_enabled", True))
                                italic_on = bool(cc.get("italic_enabled", False))
                            except Exception:
                                bold_on = True
                                italic_on = False

                            style_str = _style_from_colorizer_flags(color_hex, bold_on, italic_on)
                            colored_front = _wrap_all_clozes_with_style(raw_front, style_str)


                        model = models["cloze"]; col.models.set_current(model)
                        note = col.newNote(); note.did = deck_id
                        note["Text"] = colored_front
                        note["Back Extra"] = raw_back

                        if "SlideImage" in note and fname:
                            note["SlideImage"] = f'<img src="{fname}">'
                        note.tags.append("pdf2cards:ai_cloze")
                        if occl_tag: note.tags.append(occl_tag)
                        if not _is_real_cloze(note["Text"]):
                            _dbg("No real cloze at insertion — will fall back to Basic.")
                        else:
                            col.addNote(note); new_note_ids.append(note.id)
                            try: force_move_cards_to_deck([c.id for c in note.cards()], deck_id)
                            except Exception: pass
                            continue
                    except Exception as e:
                        _dbg(f"Cloze insert failed; falling back to Basic: {repr(e)}")

                if want_basic:
                    try:
                        model = models["basic"]; col.models.set_current(model)
                        note = col.newNote(); note.did = deck_id
                        note["Front"] = raw_front; note["Back"]  = raw_back
                        if "SlideImage" in note and fname:
                            note["SlideImage"] = f'<img src="{fname}">'
                        note.tags.append("pdf2cards:basic")
                        if occl_tag: note.tags.append(occl_tag)
                        col.addNote(note); new_note_ids.append(note.id)
                        try: force_move_cards_to_deck([c.id for c in note.cards()], deck_id)
                        except Exception: pass
                    except Exception as e:
                        _dbg(f"Basic insert failed: {repr(e)}")
                        continue

            col.save()
        except Exception as e:
            _dbg("Insert/render error: " + repr(e))
        return new_note_ids

    # ---- main: color only the newly inserted notes (optional) ----
    def _apply_color_on_main(new_nids: list):
        try:
            cfg_gen = _get_config()
            if not bool(cfg_gen.get("color_after_generation", True)):
                mw.progress.finish(); return

            try:
                from .colorizer import (
                    get_color_table, ColoringOptions, build_combined_regex,
                    apply_color_coding_to_html, _read_cfg as _cc_read_cfg
                )
            except Exception as e:
                _dbg(f"Colorizer import failed: {e}")
                mw.progress.finish(); return

            color_table = get_color_table()
            if not color_table:
                _dbg("Colorizer: empty color table — skipping.")
                mw.progress.finish(); return


            cc = _cc_read_cfg() or {}
            cc_mode = str(_get_config().get("cloze_color_mode", "per_word")).strip()
            opts_local = ColoringOptions(
                whole_words=cc.get("whole_words", True),
                case_insensitive=cc.get("case_insensitive", True),
                bold=cc.get("bold_enabled", True),
                italic=cc.get("italic_enabled", False),
                bold_plurals=cc.get("bold_plurals_enabled", True),
                colorize=cc.get("colorize_enabled", True),
                # NEW: per-word mode → color inside cloze; random/custom → keep protected
                color_inside_cloze=(cc_mode == "per_word"),
            )
            regex, group_to_color = build_combined_regex(color_table, opts_local)


            if not new_nids:
                mw.progress.finish(); return

            mw.progress.update(label=f"Coloring {len(new_nids)} new note(s)…")
            for i, nid in enumerate(new_nids, start=1):
                try:
                    note = mw.col.get_note(nid)
                    if not note: continue
                    modified = False
                    cfg_gen = _get_config()  # already read above in this function
                    cc_mode = str(cfg_gen.get("cloze_color_mode", "per_word")).strip()

                    for fname in note.keys():
                        try:
                            old = note[fname]
                            # If this is a cloze field and we used Random/Custom, preserve the single-color fill
                            # Skip recoloring *inside* cloze spans, but still color the rest of the text
                            if cc_mode in ("random_table", "custom") and fname.lower() in ("text",):
                                if _is_real_cloze(old):
                                    # Apply colorizer only OUTSIDE cloze answers
                                    try:
                                        # Split around cloze regions
                                        parts = []
                                        last_end = 0
                                        for m in _CLOZE_RE.finditer(old):
                                            # Part before the cloze → colorize normally
                                            before = old[last_end:m.start()]
                                            colored_before, _ = apply_color_coding_to_html(
                                                before, regex, group_to_color, opts_local
                                            )
                                            parts.append(colored_before)

                                            # Cloze itself → keep as-is (already wrapped with single color)
                                            parts.append(m.group(0))
                                            last_end = m.end()

                                        # Remainder after last cloze
                                        after = old[last_end:]
                                        colored_after, _ = apply_color_coding_to_html(
                                            after, regex, group_to_color, opts_local
                                        )
                                        parts.append(colored_after)

                                        new = "".join(parts)

                                        if new != old:
                                            note[fname] = new
                                            modified = True

                                        continue
                                    except Exception as e:
                                        _dbg(f"Precise cloze skip failed: {e}")
                                        # Fallback to full-skip (safe)
                                        continue
                            new, _ = apply_color_coding_to_html(old, regex, group_to_color, opts_local)
                            if new != old:
                                note[fname] = new; modified = True
                        except Exception as e:
                            _dbg(f"Colorize field '{fname}' note {nid} error: {e}")

                    if modified: note.flush()
                except Exception as e:
                    _dbg(f"Colorize note {nid} error: {e}")
                if i % 50 == 0 or i == len(new_nids):
                    mw.progress.update(label=f"Coloring… ({i}/{len(new_nids)})")
        except Exception as e:
            _dbg(f"Auto-color (new notes) failed: {repr(e)}")
        finally:
            try: mw.taskman.run_on_main(lambda: mw.progress.finish())
            except Exception: pass

    def _handle_done(fut):
        try:
            new_nids = fut.result()
        except Exception as e:
            _dbg(f"_insert_and_render failed: {e}")
            mw.taskman.run_on_main(lambda: mw.progress.finish())
            return
        mw.taskman.run_on_main(lambda: _apply_color_on_main(new_nids))

    mw.taskman.run_in_background(_insert_and_render, on_done=_handle_done)


# ──────────────────────────────────────────────────────────────────────────────
# Options dialog (per PDF), remembers toggles; resets numeric page range each time
# ──────────────────────────────────────────────────────────────────────────────




class OptionsDialog(QDialog):
    def __init__(self, parent=None, max_pages=999, default_deck_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("PDF → Cards: Options")
        self.setModal(True)
        self.cfg = _get_config().copy()  # read last-used
        self.default_deck_name = (default_deck_name or "").strip()

        # --- Size hints (fit on small screens, still resizable)
        try:
            # Fit to ~80% of Anki's main window height (safer on laptops)
            base_h = int(mw.size().height() * 0.8)
            base_w = max(560, int(mw.size().width() * 0.5))
        except Exception:
            base_w, base_h = 720, 760  # safe defaults

        self.setMinimumSize(560, 620)
        self.resize(base_w, base_h)

        # ---------------------------------------------------------------------
        # OUTER LAYOUT: scroll area (content) + button box (fixed at bottom)
        # ---------------------------------------------------------------------
        outer_v = QVBoxLayout(self)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        outer_v.addWidget(scroll, 1)

        central = QWidget(scroll)
        form_v = QVBoxLayout(central)
        central.setLayout(form_v)
        scroll.setWidget(central)

        # ---------------------------------------------------------------------
        # Deck name (top)
        # ---------------------------------------------------------------------
        from PyQt6.QtWidgets import QLineEdit
        row_deck = QHBoxLayout()
        row_deck.addWidget(QLabel("**Deck name**"))
        self.deck_edit = QLineEdit(self.default_deck_name or "")
        self.deck_edit.setPlaceholderText("e.g., Your deck name")
        row_deck.addWidget(self.deck_edit)
        form_v.addLayout(row_deck)

        # ---------------------------------------------------------------------
        # Card types
        # ---------------------------------------------------------------------
        form_v.addWidget(QLabel("**Card types**"))
        self.chk_basic = QCheckBox("Basic")
        self.chk_basic.setChecked(bool(self.cfg.get("types_basic", True)))
        self.chk_cloze = QCheckBox("Cloze (requires OpenAI cloze output)")
        self.chk_cloze.setChecked(bool(self.cfg.get("types_cloze", False)))
        form_v.addWidget(self.chk_basic)
        form_v.addWidget(self.chk_cloze)

        # ---------------------------------------------------------------------
        # Cloze coloring
        # ---------------------------------------------------------------------
        form_v.addSpacing(8)
        form_v.addWidget(QLabel("**Cloze coloring** (applies only if Cloze is selected)"))

        self.rb_cloze_perword = QRadioButton("Per‑word colorizer (multiple colors inside cloze)")
        self.rb_cloze_random  = QRadioButton("Random single color from color table")
        self.rb_cloze_custom  = QRadioButton("Custom single color…")

        cc_mode = str(self.cfg.get("cloze_color_mode", "per_word"))
        self.rb_cloze_perword.setChecked(cc_mode == "per_word")
        self.rb_cloze_random.setChecked(cc_mode == "random_table")
        self.rb_cloze_custom.setChecked(cc_mode == "custom")

        self.group_cloze_color = QButtonGroup(self)
        for rb in (self.rb_cloze_perword, self.rb_cloze_random, self.rb_cloze_custom):
            self.group_cloze_color.addButton(rb)
            form_v.addWidget(rb)

        # Custom color picker (persisted)
        from PyQt6.QtGui import QColor
        self.cloze_custom_hex = str(self.cfg.get("cloze_custom_color_hex", "#FF69B4"))
        self.btn_cloze_color = QPushButton("Pick custom color…")
        self.lbl_cloze_color = QLabel(f"Current: {self.cloze_custom_hex}")
        self.lbl_cloze_color.setStyleSheet(
            f"padding:2px 6px; border:1px solid #aaa; background:{self.cloze_custom_hex}; color:black;"
        )
        def pick_cloze_color():
            col = QColorDialog.getColor(QColor(self.cloze_custom_hex))
            if col.isValid():
                self.cloze_custom_hex = col.name()
                self.lbl_cloze_color.setText(f"Current: {self.cloze_custom_hex}")
                self.lbl_cloze_color.setStyleSheet(
                    f"padding:2px 6px; border:1px solid #aaa; background:{self.cloze_custom_hex}; color:black;"
                )
        h_ccolor = QHBoxLayout()
        h_ccolor.addWidget(self.btn_cloze_color)
        h_ccolor.addWidget(self.lbl_cloze_color)
        h_ccolor.addStretch(1)
        form_v.addLayout(h_ccolor)
        self.btn_cloze_color.clicked.connect(pick_cloze_color)

        # Enable/disable the cloze coloring section
        def _sync_cloze_color_controls():
            enabled = self.chk_cloze.isChecked()
            for w in (self.rb_cloze_perword, self.rb_cloze_random, self.rb_cloze_custom,
                      self.btn_cloze_color, self.lbl_cloze_color):
                w.setEnabled(enabled)
            # Only show color picker when "Custom" is chosen
            custom = enabled and self.rb_cloze_custom.isChecked()
            self.btn_cloze_color.setEnabled(custom)
            self.lbl_cloze_color.setEnabled(custom)

        self.chk_cloze.toggled.connect(_sync_cloze_color_controls)
        self.rb_cloze_perword.toggled.connect(_sync_cloze_color_controls)
        self.rb_cloze_random.toggled.connect(_sync_cloze_color_controls)
        self.rb_cloze_custom.toggled.connect(_sync_cloze_color_controls)
        _sync_cloze_color_controls()

        # ---------------------------------------------------------------------
        # Occlusion
        # ---------------------------------------------------------------------
        self.chk_occl = QCheckBox("Auto-occlusion near images (AI) (not working at the moment)")
        self.chk_occl.setChecked(bool(self.cfg.get("occlusion_enabled", True)))
        form_v.addWidget(self.chk_occl)

        # ---------------------------------------------------------------------
        # Cards per slide
        # ---------------------------------------------------------------------
        form_v.addSpacing(8)
        form_v.addWidget(QLabel("**Cards per slide**"))

        self.rb_all = QRadioButton("AI decides (all cards returned)")
        self.rb_range = QRadioButton("Range (random subset per slide)")
        mode = self.cfg.get("per_slide_mode", "ai")
        self.rb_all.setChecked(mode == "ai")
        self.rb_range.setChecked(mode == "range")
        self.group_cards = QButtonGroup(self)
        self.group_cards.addButton(self.rb_all)
        self.group_cards.addButton(self.rb_range)
        form_v.addWidget(self.rb_all)
        form_v.addWidget(self.rb_range)

        h_per = QHBoxLayout()
        self.spin_min = QSpinBox()
        self.spin_max = QSpinBox()
        self.spin_min.setRange(1, 50)
        self.spin_max.setRange(1, 50)
        self.spin_min.setValue(int(self.cfg.get("per_slide_min", 1)))
        self.spin_max.setValue(int(self.cfg.get("per_slide_max", 3)))
        h_per.addWidget(QLabel("Min:"))
        h_per.addWidget(self.spin_min)
        h_per.addWidget(QLabel("Max:"))
        h_per.addWidget(self.spin_max)
        form_v.addLayout(h_per)

        def sync_per():
            enabled = self.rb_range.isChecked()
            self.spin_min.setEnabled(enabled)
            self.spin_max.setEnabled(enabled)
        self.rb_all.toggled.connect(sync_per)
        self.rb_range.toggled.connect(sync_per)
        sync_per()

        # ---------------------------------------------------------------------
        # Page selection (numeric range resets every time)
        # ---------------------------------------------------------------------
        form_v.addSpacing(10)
        form_v.addWidget(QLabel("**Pages (applies to the selected PDF)**"))
        self.rb_pages_all = QRadioButton("All pages (default)")
        self.rb_pages_range = QRadioButton("Page range")
        self.rb_pages_all.setChecked(self.cfg.get("page_mode","all") == "all")
        self.rb_pages_range.setChecked(self.cfg.get("page_mode","all") == "range")
        self.group_pages = QButtonGroup(self)
        self.group_pages.addButton(self.rb_pages_all)
        self.group_pages.addButton(self.rb_pages_range)
        form_v.addWidget(self.rb_pages_all)
        form_v.addWidget(self.rb_pages_range)

        h_pages = QHBoxLayout()
        self.spin_page_from = QSpinBox()
        self.spin_page_to   = QSpinBox()
        self.spin_page_from.setRange(1, max_pages)
        self.spin_page_to.setRange(1, max_pages)
        self.spin_page_from.setValue(1)
        self.spin_page_to.setValue(max_pages)
        h_pages.addWidget(QLabel("From:"))
        h_pages.addWidget(self.spin_page_from)
        h_pages.addWidget(QLabel("To:"))
        h_pages.addWidget(self.spin_page_to)
        form_v.addLayout(h_pages)

        def sync_pages():
            enabled = self.rb_pages_range.isChecked()
            self.spin_page_from.setEnabled(enabled)
            self.spin_page_to.setEnabled(enabled)
        self.rb_pages_all.toggled.connect(sync_pages)
        self.rb_pages_range.toggled.connect(sync_pages)
        sync_pages()

        # ---------------------------------------------------------------------
        # Highlight options
        # ---------------------------------------------------------------------
        self.chk_highlight = QCheckBox("Highlight used text on slide")
        self.chk_highlight.setChecked(bool(self.cfg.get("highlight_enabled", True)))
        form_v.addWidget(self.chk_highlight)

        # Opacity sliders (persisted)
        op_row = QHBoxLayout()
        op_row.addWidget(QLabel("Fill opacity (0–255):"))
        self.spin_fill = QSpinBox()
        self.spin_fill.setRange(0, 255)
        self.spin_fill.setValue(int(self.cfg.get("highlight_fill_alpha", 140)))
        op_row.addWidget(self.spin_fill)

        op_row.addSpacing(12)
        op_row.addWidget(QLabel("Outline opacity (0–255):"))
        self.spin_outline = QSpinBox()
        self.spin_outline.setRange(0, 255)
        self.spin_outline.setValue(int(self.cfg.get("highlight_outline_alpha", 230)))
        op_row.addWidget(self.spin_outline)
        op_row.addStretch(1)
        form_v.addLayout(op_row)

        # Color picker
        self.btn_color = QPushButton("Highlight color…")
        self.color_hex = str(self.cfg.get("highlight_color_hex", "#FF69B4"))
        self.lbl_color = QLabel(f"Current: {self.color_hex}")
        self.lbl_color.setStyleSheet(
            f"padding:2px 6px; border:1px solid #aaa; background:{self.color_hex}; color:black;"
        )
        from PyQt6.QtGui import QColor
        def pick_color():
            col = QColorDialog.getColor(QColor(self.color_hex))
            if col.isValid():
                self.color_hex = col.name()
                self.lbl_color.setText(f"Current: {self.color_hex}")
                self.lbl_color.setStyleSheet(
                    f"padding:2px 6px; border:1px solid #aaa; background:{self.color_hex}; color:black;"
                )
        h_color = QHBoxLayout()
        h_color.addWidget(self.btn_color)
        h_color.addWidget(self.lbl_color)
        h_color.addStretch(1)
        form_v.addLayout(h_color)
        self.btn_color.clicked.connect(pick_color)

        # Coloring shortcuts (open colorizer UIs from here)
        form_v.addSpacing(8)
        form_v.addWidget(QLabel("**Coloring (deck/text highlighting)**"))
        row_col = QHBoxLayout()
        self.btn_color_table = QPushButton("Color Table…")
        self.btn_color_settings = QPushButton("Coloration Settings…")
        row_col.addWidget(self.btn_color_table)
        row_col.addWidget(self.btn_color_settings)
        row_col.addStretch(1)
        form_v.addLayout(row_col)
        def _connect_colorizer():
            try:
                # Imported earlier at top of file:
                # from .colorizer import open_coloration_settings_dialog, on_edit_color_table
                self.btn_color_table.clicked.connect(on_edit_color_table)
                self.btn_color_settings.clicked.connect(open_coloration_settings_dialog)
            except Exception:
                self.btn_color_table.setEnabled(False)
                self.btn_color_settings.setEnabled(False)
        _connect_colorizer()

        # Post-generation coloring toggles (persist)
        self.chk_color_after = QCheckBox("Color deck after generation")
        self.chk_color_after.setChecked(bool(self.cfg.get("color_after_generation", True)))
        form_v.addWidget(self.chk_color_after)

        self.chk_ai_extend = QCheckBox("Let AI extend color table before coloring")
        self.chk_ai_extend.setChecked(bool(self.cfg.get("ai_extend_color_table", True)))
        form_v.addWidget(self.chk_ai_extend)

        # ---------------------------------------------------------------------
        # Button box (ONLY thing that goes on outer_v below the scroll area)
        # ---------------------------------------------------------------------
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self
        )
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        outer_v.addWidget(btn_box, 0)

    def options(self) -> dict:
        """Persist everything except numeric page range, and return run options."""
        c = _get_config()
        c["types_basic"]  = self.chk_basic.isChecked()
        c["types_cloze"]  = self.chk_cloze.isChecked()
        c["highlight_enabled"]    = self.chk_highlight.isChecked()
        c["highlight_color_hex"]  = self.color_hex
        c["highlight_fill_alpha"] = int(self.spin_fill.value())       # NEW
        c["highlight_outline_alpha"] = int(self.spin_outline.value()) # NEW
        c["occlusion_enabled"] = self.chk_occl.isChecked()

        mode = "range" if self.rb_range.isChecked() else "ai"
        c["per_slide_mode"] = mode
        c["per_slide_min"]  = int(self.spin_min.value())
        c["per_slide_max"]  = int(self.spin_max.value())

        c["page_mode"]      = "range" if self.rb_pages_range.isChecked() else "all"
        c["color_after_generation"] = self.chk_color_after.isChecked()
        c["ai_extend_color_table"]  = self.chk_ai_extend.isChecked()

        mode = "per_word"
        if self.rb_cloze_random.isChecked():
            mode = "random_table"
        elif self.rb_cloze_custom.isChecked():
            mode = "custom"
        c["cloze_color_mode"] = mode
        c["cloze_custom_color_hex"] = self.cloze_custom_hex

        _save_config(c)

        return {
            "highlight_enabled": c["highlight_enabled"],
            "highlight_color_hex": c["highlight_color_hex"],
            "highlight_fill_alpha": c["highlight_fill_alpha"],           # NEW
            "highlight_outline_alpha": c["highlight_outline_alpha"],     # NEW
            "types_basic": c["types_basic"],
            "types_cloze": c["types_cloze"],
            "per_slide_mode": c["per_slide_mode"],
            "per_slide_min": c["per_slide_min"],
            "per_slide_max": c["per_slide_max"],
            "page_mode": c["page_mode"],
            "page_from": int(self.spin_page_from.value()),
            "page_to":   int(self.spin_page_to.value()),
            "occlusion_enabled": c["occlusion_enabled"],
            "cloze_color_mode": c["cloze_color_mode"],
            "cloze_custom_color_hex": c["cloze_custom_color_hex"],
            "deck_name": (self.deck_edit.text() or "").strip(),
        }



# ──────────────────────────────────────────────────────────────────────────────
# API key prompt (persisted)
# ──────────────────────────────────────────────────────────────────────────────

def get_api_key():
    config = _get_config()
    key = (config.get("openai_api_key") or "").strip()
    if key:
        return key
    key, ok = QInputDialog.getText(mw, "OpenAI API Key", "Paste your OpenAI API key:")
    if not ok or not key:
        return None
    config["openai_api_key"] = key.strip(); _save_config(config)
    return key.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Main command — pick PDFs → per-file Options → launch worker
# (with progress bar repaint before worker starts)
# ──────────────────────────────────────────────────────────────────────────────

def generate_from_pdf():
    api_key = get_api_key()
    if not api_key:
        return

    pdf_paths, _ = QFileDialog.getOpenFileNames(mw, "Select PDF(s)", "", "PDF files (*.pdf)")
    if not pdf_paths:
        return

    # If user picked multiple PDFs, ask ONCE for a global deck name
    global_deck_name = None
    if len(pdf_paths) > 1:
        from PyQt6.QtWidgets import QInputDialog
        # Default from the first file's name
        default_deck_name = deck_name_from_pdf_path(pdf_paths[0])
        deck_name_text, ok = QInputDialog.getText(
            mw,
            "Deck name for all selected PDFs",
            "Cards from ALL selected PDFs will be added to this deck:",
            text=default_deck_name,
        )
        if not ok:
            return
        global_deck_name = (deck_name_text or default_deck_name).strip()

    for pdf_path in pdf_paths:
        # Compute defaults based on the file
        pdf_name = os.path.basename(pdf_path)
        default_deck_name = deck_name_from_pdf_path(pdf_path)  # e.g., file name without extension

        # Per-PDF page count: resets range to 1..last in dialog
        max_pages = _pdf_page_count(pdf_path) or 999

        # Show Options dialog (still useful for per-file page ranges & toggles)
        # We continue to show the deck field for single-file runs; it's ignored when global_deck_name is set.
        dlg = OptionsDialog(mw, max_pages=max_pages, default_deck_name=default_deck_name)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            continue

        opts = dlg.options()
        if not (opts.get("types_basic") or opts.get("types_cloze")):
            showWarning("Select at least one card type (Basic or Cloze). Aborting.")
            continue

        # Use the global deck name if present; otherwise, fall back to the dialog or default
        deck_name = (global_deck_name or opts.get("deck_name") or default_deck_name).strip()
        deck_id = get_or_create_deck(deck_name)
        mw.col.decks.select(deck_id)

        # Ensure models based on chosen options
        models = {}
        if opts.get("types_basic"):
            models["basic"] = get_basic_model_fallback() or ensure_basic_with_slideimage("Basic + Slide")
        if opts.get("types_cloze"):
            models["cloze"] = ensure_cloze_with_slideimage("Cloze + Slide")
        else:
            models["cloze"] = mw.col.models.byName("Cloze + Slide") or ensure_cloze_with_slideimage("Cloze + Slide")

        # Quick “Preparing …” progress repaint
        mw.taskman.run_on_main(lambda: mw.progress.start(label=f"Preparing {pdf_name}…", immediate=True))

        # Worker completion wrapper closes quick bar and continues
        def on_done_wrapper(deck_id=deck_id, deck_name=deck_name):
            def _on_done(fut):
                try:
                    mw.taskman.run_on_main(lambda: mw.progress.finish())
                except Exception:
                    pass
                try:
                    res = fut.result()
                except Exception as e:
                    tb = traceback.format_exc()
                    tb_snip = tb[:1200] + "\n…(truncated)…" if len(tb) > 1200 else tb
                    mw.progress.finish()
                    showWarning(f"Generation failed.\n\nError: {e}\n\n{tb_snip}")
                    return
                _on_worker_done(res, deck_id, deck_name, models, opts)
            return _on_done

        # Launch worker
        mw.taskman.run_in_background(
            lambda p=pdf_path, k=api_key, o=opts: _worker_generate_cards(p, k, o),
            on_done=on_done_wrapper(),
        )


# ──────────────────────────────────────────────────────────────────────────────
# Menu entry
# ──────────────────────────────────────────────────────────────────────────────

def init_addon():
    action = QAction("Generate Anki cards from PDF", mw)
    action.triggered.connect(generate_from_pdf)
    mw.form.menuTools.addAction(action)