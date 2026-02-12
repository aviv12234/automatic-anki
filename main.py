# main.py — Cleaned for Pure Qt Rendering (no PyMuPDF anywhere)

import os
import re
import sys
import random
import traceback
import time
from typing import List, Dict, Optional, Tuple
from io import BytesIO

from .colorizer import open_coloration_settings_dialog
from .colorizer import on_edit_color_table

from aqt import mw
from aqt.utils import showWarning

from aqt.qt import (
    QAction, QFileDialog, QInputDialog, QMessageBox, QDialog, QVBoxLayout,
    QHBoxLayout, QLabel, QCheckBox, QRadioButton, QSpinBox, QPushButton,
    QButtonGroup, QColorDialog
)

# Rendering + OCR imports
from .pdf_parser import extract_words_with_boxes, boxes_for_phrase, sentence_rects_for_phrase
from .pdf_parser import extract_image_boxes
from .pdf_parser import extract_text_from_pdf

from .pdf_images import render_page_as_png
from .pdf_images import render_page_as_png_with_highlights

from .openai_cards import suggest_occlusions_from_image
from .openai_cards import generate_cards

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OCCLUSION_ENABLED = True
_OCCLUSION_DPI = 200
_IMAGE_MARGIN_PDF_PT = 36.0   # ~0.5 inch
_MAX_MASKS_PER_CROP = 12

ADDON_ID = os.path.basename(os.path.dirname(__file__))

# ---------------------------------------------------------------------------
# Debug logger
# ---------------------------------------------------------------------------



def _dbg(msg: str) -> None:
    """Append timestamped debug line to profile log."""
    try:
        path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# PNG resizing for Vision API (Qt-only)
# ---------------------------------------------------------------------------

def _pdf_page_count(pdf_path: str) -> int:
    """Return page count via QtPdf (needs parent=None) or pypdf fallback."""
    # Try QtPdf first
    try:
        from PyQt6.QtPdf import QPdfDocument
        qdoc = QPdfDocument(None)     # IMPORTANT: PyQt6 requires a parent argument
        qdoc.load(pdf_path)
        if qdoc.status() == QPdfDocument.Status.Ready:
            return int(qdoc.pageCount())
    except Exception:
        pass

    # Fallback: pypdf (pure python)
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        return len(reader.pages)
    except Exception:
        return 0

def _limit_png_size_for_vision(png_bytes: bytes, max_bytes: int = 3_500_000) -> bytes:
    """Shrink PNG using Qt only (no PIL)."""
    if not png_bytes or len(png_bytes) <= max_bytes:
        return png_bytes

    try:
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice

        img = QImage.fromData(png_bytes)
        if img.isNull():
            return png_bytes

        w, h = img.width(), img.height()
        scale = 0.85
        for _ in range(8):
            w = max(1, int(w * scale))
            h = max(1, int(h * scale))
            small = img.scaled(w, h)

            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            small.save(buf, b"PNG")
            buf.close()

            out = bytes(ba)
            if len(out) <= max_bytes:
                return out

            img = small

        return out
    except Exception:
        return png_bytes

# ---------------------------------------------------------------------------
# Media helper
# ---------------------------------------------------------------------------

def _write_media_file(basename: str, data: bytes) -> Optional[str]:
    """Store bytes in Anki media. Return stored filename or None."""
    try:
        return mw.col.media.write_data(basename, data)
    except Exception:
        # try temp fallback
        try:
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), basename)
            with open(tmp, "wb") as f:
                f.write(data)
            return mw.col.media.add_file(tmp)
        except Exception:
            return None

# ---------------------------------------------------------------------------
# Qt-only crop (used by occlusion snapshot)
# ---------------------------------------------------------------------------

def _crop_png_region(png_bytes: bytes, rect_pt: dict, dpi: int) -> bytes:
    """Crop using Qt only."""
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

    if x + w > img.width():
        w = img.width() - x
    if y + h > img.height():
        h = img.height() - y
    if w <= 0 or h <= 0:
        return b""

    cropped = img.copy(x, y, w, h)

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    cropped.save(buf, b"PNG")
    buf.close()
    return bytes(ba)

# ---------------------------------------------------------------------------
# Mask rectangle on PNG (Qt-only)
# ---------------------------------------------------------------------------

def _mask_one_rect_on_png(png_bytes: bytes, rect_px: dict,
                          fill=(242, 242, 242), outline=(160, 160, 160)) -> bytes:
    """Draw mask rectangle using Qt only."""
    if not png_bytes:
        return b""

    from PyQt6.QtGui import QImage, QPainter, QColor, QPen, QBrush
    from PyQt6.QtCore import QByteArray, QBuffer, QIODevice, QRect

    img = QImage.fromData(png_bytes)
    if img.isNull():
        return png_bytes

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    painter.setBrush(QBrush(QColor(*fill)))
    pen = QPen(QColor(*outline))
    pen.setWidth(2)
    painter.setPen(pen)

    x = int(rect_px.get("x", 0))
    y = int(rect_px.get("y", 0))
    w = int(rect_px.get("w", 0))
    h = int(rect_px.get("h", 0))

    if w > 0 and h > 0:
        painter.drawRect(QRect(x, y, w, h))

    painter.end()

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, b"PNG")
    buf.close()
    return bytes(ba)

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_config() -> dict:
    c = mw.addonManager.getConfig(ADDON_ID) or {}
    c.setdefault("highlight_enabled", True)
    c.setdefault("highlight_color_hex", "#FF69B4")
    c.setdefault("occlusion_enabled", True)
    c.setdefault("openai_api_key", "")
    c.setdefault("types_basic", True)
    c.setdefault("types_cloze", False)
    c.setdefault("per_slide_mode", "ai")
    c.setdefault("per_slide_min", 1)
    c.setdefault("per_slide_max", 3)
    return c

def _save_config(c: dict) -> None:
    mw.addonManager.writeConfig(ADDON_ID, c)

# ---------------------------------------------------------------------------
# Deck helpers
# ---------------------------------------------------------------------------

def deck_name_from_pdf_path(pdf_path: str) -> str:
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return base.strip()

def get_or_create_deck(deck_name: str) -> int:
    col = mw.col
    did = col.decks.id(deck_name, create=False)
    if did:
        return did
    return col.decks.id(deck_name, create=True)

def _rgba_from_hex(hex_str: str, alpha: int = 55):
    s = (hex_str or "").strip()
    if not s.startswith("#") or len(s) != 7:
        s = "#FF69B4"
    r = int(s[1:3], 16)
    g = int(s[3:5], 16)
    b = int(s[5:7], 16)
    return (r, g, b, int(alpha))

# ---------------------------------------------------------------------------
# Note model setup
# ---------------------------------------------------------------------------

def ensure_basic_with_slideimage(model_name: str = "Basic + Slide") -> dict:
    col = mw.col
    m = col.models.byName(model_name)
    created = False

    if not m:
        m = col.models.new(model_name)
        created = True

    want_fields = ["Front", "Back", "SlideImage"]
    have = [f.get("name") for f in (m.get("flds") or [])]
    for name in want_fields:
        if name not in have:
            col.models.addField(m, col.models.newField(name))

    qfmt = "{{Front}}"
    afmt = "{{FrontSide}}\n\n<hr>\n{{Back}}\n\n<hr>\n{{SlideImage}}"

    tmpls = m.get("tmpls") or []
    if not tmpls:
        t = col.models.newTemplate("Card 1")
        t["qfmt"] = qfmt
        t["afmt"] = afmt
        col.models.addTemplate(m, t)
    else:
        t = tmpls[0]
        t["qfmt"] = qfmt
        t["afmt"] = afmt
        m["tmpls"][0] = t

    m["css"] = (
    ".card { font-family: arial; font-size: 20px; text-align: center; "
    "color: black; background-color: white; overflow:auto !important; }\n"
    ## Make images responsive to the card width 
    ".card img { "
    "  max-width: 96vw !important;"   ## cap to viewport width 
    "  width: auto !important;"
    "  height: auto;"
    "  image-rendering: crisp-edges;"
    "}\n"
)


    if created:
        col.models.add(m)
    else:
        col.models.save(m)
    return col.models.byName(model_name)

def get_basic_model_fallback():
    col = mw.col
    m = col.models.byName("Basic")
    if m:
        return m
    for cand in col.models.all():
        if len(cand.get("flds", [])) >= 2 and len(cand.get("tmpls", [])) >= 1:
            return cand
    return col.models.current()
def ensure_cloze_with_slideimage(model_name: str = "Cloze + Slide") -> dict:
    col = mw.col
    m = col.models.byName(model_name)

    # Always enforce template + fields, even if model already exists
    def enforce_template(model):
        want_fields = ["Text", "Back Extra", "SlideImage"]

        # ensure all fields exist
        have = [f["name"] for f in model.get("flds", [])]
        for name in want_fields:
            if name not in have:
                col.models.addField(model, col.models.newField(name))

        # enforce templates
        tmpls = model.get("tmpls") or []
        if not tmpls:
            t = col.models.newTemplate("Cloze")
            tmpls.append(t)

        t = tmpls[0]
        t["qfmt"] = "{{cloze:Text}}"
        t["afmt"] = (
            "{{cloze:Text}}\n\n"
            "{{#Back Extra}}{{Back Extra}}{{/Back Extra}}\n\n"
            "<hr>\n"
            "{{SlideImage}}"
        )
        model["tmpls"][0] = t

        # enforce css

        model["css"] = (
            ".card { font-family: arial; font-size: 20px; text-align: center; "
            "color: black; background-color: white; overflow:auto !important; }\n"
            ".card img { "
            "  max-width: 96vw !important;"   
            "  width: auto !important;"
            "  height: auto;"
            "  image-rendering: crisp-edges;"
            "}\n"
        )


        col.models.save(model)
        return model

    # Create if missing
    if not m:
        m = col.models.new(model_name)
        m["type"] = 1
        col.models.add(m)

    # Always enforce
    return enforce_template(m)

# ---------------------------------------------------------------------------
# Card-moving helper
# ---------------------------------------------------------------------------

def force_move_cards_to_deck(cids: list, deck_id: int):
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

# ---------------------------------------------------------------------------
# Background worker — PDF → text → cards
# ---------------------------------------------------------------------------

def _worker_generate_cards(pdf_path: str, api_key: str, opts: dict) -> Dict:
    _dbg(f"WORKER START: pdf={pdf_path}, opts={opts}")

    def ui_update(label: str):
        try:
            mw.progress.update(label=label)
        except Exception:
            pass

    try:
        page_mode = opts.get("page_mode", "all")

        if page_mode == "range":
            page_from = max(1, int(opts.get("page_from", 1)))
            page_to = int(opts.get("page_to", 10**9))
            if page_to < page_from:
                page_from, page_to = page_to, page_from
            max_pages = page_to - page_from + 1
            pages = extract_text_from_pdf(pdf_path, api_key,
                                          page_start=page_from,
                                          max_pages=max_pages)
        else:
            _dbg("Calling extract_text_from_pdf()...")
            pages = extract_text_from_pdf(pdf_path, api_key)

        _dbg(f"extract_text_from_pdf returned {len(pages)} pages")

        if not pages:
            _dbg("No pages returned! Exiting worker.")
            return {
                "ok": True, "cards": [], "pages": 0,
                "errors": [], "meta": {"pdf_path": pdf_path}
            }

        total_pages = len(pages)
        results: List[dict] = []
        page_errors: List[str] = []

        mode = opts.get("per_slide_mode", "ai")
        minv = int(opts.get("per_slide_min", 1))
        maxv = int(opts.get("per_slide_max", 3))
        if maxv < minv:
            minv, maxv = maxv, minv

        for idx, page in enumerate(pages, start=1):
            mw.taskman.run_on_main(
                lambda i=idx, t=total_pages:
                ui_update(f"Processing page {i} of {t}")
            )

            text = (page.get("text") or "").strip()
            if not text:
                _dbg(f"No OCR text on page {page.get('page')} — skipping")
                continue

            _dbg(f"Generating cards for page {page['page']}: {len(text)} chars")

            # 1. Get content cards
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

            # Range trimming
            if mode == "range" and cards:
                n = max(0, min(random.randint(minv, maxv), len(cards)))
                cloze_first = []
                non_cloze = []
                for c in cards:
                    f = (c.get("front") or "")
                    b = (c.get("back") or "")
                    if "{{c" in (f + b):
                        cloze_first.append(c)
                    else:
                        non_cloze.append(c)
                cards = (cloze_first + non_cloze)[:n]

            # 2. Occlusion (not modified here)
            try:
                if bool(opts.get("occlusion_enabled", True)):
                    img_boxes = extract_image_boxes(pdf_path, page["page"])
                    occl_cards = []

                    page_png = render_page_as_png(
                        pdf_path, page["page"],
                        dpi=_OCCLUSION_DPI, max_width=4000
                    ) or b""

                    for r_idx, ib in enumerate(img_boxes, start=1):
                        rect_pt = {
                            "x": max(0.0, ib["x"] - _IMAGE_MARGIN_PDF_PT),
                            "y": max(0.0, ib["y"] - _IMAGE_MARGIN_PDF_PT),
                            "w": ib["w"] + 2.0 * _IMAGE_MARGIN_PDF_PT,
                            "h": ib["h"] + 2.0 * _IMAGE_MARGIN_PDF_PT,
                        }

                        crop_png = _crop_png_region(
                            page_png, rect_pt, dpi=_OCCLUSION_DPI
                        )
                        if not crop_png:
                            continue

                        out = suggest_occlusions_from_image(
                            crop_png, api_key,
                            max_masks=_MAX_MASKS_PER_CROP, temperature=0.0
                        )

                        masks_px = (out.get("masks") if isinstance(out, dict) else []) or []

                        for i, m in enumerate(masks_px, start=1):
                            masked_png = _mask_one_rect_on_png(crop_png, m)

                            occl_cards.append({
                                "front": "",
                                "back": "",
                                "page": page["page"],
                                "hi": [],
                                "_occl_assets": {
                                    "base_crop_bytes": crop_png,
                                    "masked_bytes": masked_png,
                                    "base_name": f"occl_p{page['page']}_r{r_idx}_base.png",
                                    "masked_name": f"occl_p{page['page']}_r{r_idx}_m{i}.png",
                                },
                                "_occl_tag": "pdf2cards:ai_occlusion"
                            })

                    cards += occl_cards

            except Exception as e:
                _dbg("Auto-occlusion error: " + repr(e))

            # 3. Sentence highlight regions
            try:
                page_words = extract_words_with_boxes(pdf_path, page["page"])
            except Exception:
                page_words = []

            for card in cards:
                # occlusion card passthrough
                if card.get("_occl_assets"):
                    results.append({
                        "front": card.get("front", ""),
                        "back": card.get("back", ""),
                        "page": page["page"],
                        "hi": [],
                        "_occl_assets": card["_occl_assets"],
                        "_occl_tag": card.get("_occl_tag")
                    })
                    continue

                front = (card.get("front") or "").strip()
                back = (card.get("back") or "").strip()
                phrase = back or front

                hi_rects = []
                if opts.get("highlight_enabled", True):
                    rects = sentence_rects_for_phrase(
                        page_words, phrase, max_sentences=1
                    )
                    if not rects:
                        rects = boxes_for_phrase(page_words, phrase)
                    hi_rects = rects or []

                results.append({
                    "front": front,
                    "back": back,
                    "page": page["page"],
                    "hi": hi_rects,
                })

        return {
            "ok": True,
            "cards": results,
            "pages": total_pages,
            "errors": page_errors,
            "meta": {"pdf_path": pdf_path},
        }

    except Exception as e:
        tb = traceback.format_exc()
        return {
            "ok": False,
            "cards": [],
            "pages": 0,
            "errors": [],
            "error": str(e),
            "traceback": tb,
        }

# ---------------------------------------------------------------------------
# After worker completes — insert notes + render images
# ---------------------------------------------------------------------------
def _on_worker_done(result: Dict, deck_id: int, deck_name: str,
                    models: Dict[str, dict], opts: dict):

    # ---- 0) Fail fast on worker errors ----
    if not isinstance(result, dict) or not result.get("ok", False):
        tb = result.get("traceback", "") if isinstance(result, dict) else ""
        tb_snip = tb[:1200] + "\n…(truncated)…" if tb and len(tb) > 1200 else tb
        mw.progress.finish()
        showWarning(f"Generation failed.\n\nError: {result.get('error')}\n\n{tb_snip}")
        return

    cards = result.get("cards", []) or []
    pdf_path = result.get("meta", {}).get("pdf_path")
    _dbg(f"Worker produced {len(cards)} cards total")  # ok
    if not cards:
        mw.progress.finish()
        showWarning(
            "No cards were generated.\n\n"
            "Possible reasons:\n"
            "• OCR returned empty text (see pdf2cards_debug.log)\n"
            "• OpenAI returned no cards\n"
            "Try a smaller test or a page range.\n"
        )
        return

    want_basic = bool(opts.get("types_basic", True))
    want_cloze = bool(opts.get("types_cloze", False))
    if not (want_basic or want_cloze):
        mw.progress.finish()
        showWarning("No card types selected. Aborting.")
        return

    # Ensure models exist and are enforced every run
    if want_basic:
        models["basic"] = ensure_basic_with_slideimage(
            models.get("basic", {}).get("name", "Basic + Slide"))
    if want_cloze:
        models["cloze"] = ensure_cloze_with_slideimage(
            models.get("cloze", {}).get("name", "Cloze + Slide"))

    mw.progress.start(label=f"Inserting {len(cards)} card(s)…", immediate=True)

    # ---------------------------------------------------------
    # 1) Background: insert notes + render slide image (fast)
    #    -> returns list of new note ids
    # ---------------------------------------------------------
    def _insert_and_render() -> list:
        new_note_ids: list = []

        try:
            total = len(cards)

            # Strict cloze detector — only {{cN::...}} qualifies
            def _is_real_cloze(text: str) -> bool:
                import re
                return bool(re.search(r"\{\{c\d+::.+?\}\}", text or ""))

            for idx, card in enumerate(cards, start=1):
                mw.taskman.run_on_main(
                    lambda i=idx, t=total:
                    mw.progress.update(label=f"Rendering cards… ({i}/{t})")
                )

                front = card.get("front", "") or ""
                back = card.get("back", "") or ""
                page_no = card.get("page")
                hi_rects = card.get("hi", []) or []
                fname = ""
                occl_tag = None

                # -------------------------
                # Occlusion image handling
                # -------------------------
                assets = card.get("_occl_assets")
                if assets:
                    base_name = assets.get("base_name") or "occl_base.png"
                    masked_name = assets.get("masked_name") or "occl_masked.png"
                    base_path = _write_media_file(base_name, assets.get("base_crop_bytes") or b"")
                    masked_path = _write_media_file(masked_name, assets.get("masked_bytes") or b"")
                    if not (base_path and masked_path):
                        _dbg("Occlusion: failed to store media; skipping card.")
                        continue
                    base_fn = os.path.basename(base_path)
                    masked_fn = os.path.basename(masked_path)
                    front = f'<img src="{masked_fn}">'
                    back = f'<img src="{base_fn}">'
                    occl_tag = card.get("_occl_tag")

                # -------------------------------------------------
                # Render slide image (with highlights if enabled)
                # -------------------------------------------------
                if pdf_path and page_no:
                    try:
                        if opts.get("highlight_enabled", True) and not assets:

                            # AFTER (stronger fill + vivid outline)
                            fill_rgba    = _rgba_from_hex(opts.get("highlight_color_hex", "#FF69B4"), alpha=100)  # 0..255
                            outline_rgba = _rgba_from_hex(opts.get("highlight_color_hex", "#FF69B4"), alpha=230)  # 0..255

                            _dbg(f"HIs: page={page_no} rects={len(hi_rects)}")
                            png = render_page_as_png_with_highlights(
                                pdf_path, page_no, hi_rects,
                                dpi=300, max_width=4000,
                                fill_rgba=fill_rgba,
                                outline_rgba=outline_rgba,
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

                # -------------------------
                # Insert note(s)
                # -------------------------
                col = mw.col
                raw_front = (front or "").strip()
                raw_back = (back or "").strip()
                is_cloze = _is_real_cloze(raw_front) or _is_real_cloze(raw_back)

                # Escape curly braces ONLY on Basic (avoid template tidy issues).
                # Keep braces intact for real Cloze.
                if not is_cloze:
                    # minimal HTML entity escaping for braces
                    raw_front = raw_front.replace("{", "&#123;").replace("}", "&#125;")
                    raw_back  = raw_back.replace("{", "&#123;").replace("}", "&#125;")

                # ---- Cloze branch (only if real cloze + user enabled) ----
                if is_cloze and want_cloze:
                    try:
                        model = models["cloze"]
                        col.models.set_current(model)
                        note = col.newNote()
                        note.did = deck_id
                        note["Text"] = raw_front
                        note["Back Extra"] = raw_back
                        if "SlideImage" in note and fname:
                            note["SlideImage"] = f'<img src="{fname}">'
                        note.tags.append("pdf2cards:ai_cloze")
                        if occl_tag:
                            note.tags.append(occl_tag)
                        if not _is_real_cloze(note["Text"]):
                            _dbg("No real cloze at insertion — will fall back to Basic.")
                        else:
                            col.addNote(note)
                            new_note_ids.append(note.id)
                            try:
                                force_move_cards_to_deck([c.id for c in note.cards()], deck_id)
                            except Exception:
                                pass
                            continue  # next card
                    except Exception as e:
                        _dbg(f"Cloze insert failed; falling back to Basic: {repr(e)}")
                        # fall through

                # ---- Basic branch ----
                if want_basic:
                    try:
                        model = models["basic"]
                        col.models.set_current(model)
                        note = col.newNote()
                        note.did = deck_id
                        note["Front"] = raw_front
                        note["Back"] = raw_back
                        if "SlideImage" in note and fname:
                            note["SlideImage"] = f'<img src="{fname}">'
                        note.tags.append("pdf2cards:basic")
                        if occl_tag:
                            note.tags.append(occl_tag)
                        col.addNote(note)
                        new_note_ids.append(note.id)
                        try:
                            force_move_cards_to_deck([c.id for c in note.cards()], deck_id)
                        except Exception:
                            pass
                    except Exception as e:
                        _dbg(f"Basic insert failed: {repr(e)}")
                        continue

            col.save()
        except Exception as e:
            _dbg("Insert/render error: " + repr(e))

        return new_note_ids

    # ---------------------------------------------------------
    # 2) Main thread: optional colorize ONLY the new notes
    # ---------------------------------------------------------
    def _apply_color_on_main(new_nids: list):
        try:
            # Respect the user's toggle from the Options dialog
            cfg_gen = _get_config()
            if not bool(cfg_gen.get("color_after_generation", True)):
                mw.progress.finish()
                return

            # Pull colorizer settings + table
            try:
                from .colorizer import (
                    get_color_table, ColoringOptions, build_combined_regex,
                    apply_color_coding_to_html, _read_cfg as _cc_read_cfg
                )
            except Exception as e:
                _dbg(f"Colorizer import failed: {e}")
                mw.progress.finish()
                return

            color_table = get_color_table()
            if not color_table:
                _dbg("Colorizer: empty color table — skipping.")
                mw.progress.finish()
                return

            cc = _cc_read_cfg() or {}
            opts_local = ColoringOptions(
                whole_words=cc.get("whole_words", True),
                case_insensitive=cc.get("case_insensitive", True),
                bold=cc.get("bold_enabled", True),
                italic=cc.get("italic_enabled", False),
                bold_plurals=cc.get("bold_plurals_enabled", True),
                colorize=cc.get("colorize_enabled", True),
            )
            regex, group_to_color = build_combined_regex(color_table, opts_local)

            if not new_nids:
                mw.progress.finish()
                return

            mw.progress.update(label=f"Coloring {len(new_nids)} new note(s)…")

            for i, nid in enumerate(new_nids, start=1):
                try:
                    note = mw.col.get_note(nid)
                    if not note:
                        continue
                    modified = False
                    for fname in note.keys():
                        old = note[fname]
                        new, _ = apply_color_coding_to_html(old, regex, group_to_color, opts_local)
                        if new != old:
                            note[fname] = new
                            modified = True
                    if modified:
                        note.flush()
                except Exception as e:
                    _dbg(f"Colorize note {nid} error: {e}")

                if i % 50 == 0 or i == len(new_nids):
                    mw.progress.update(label=f"Coloring… ({i}/{len(new_nids)})")

        except Exception as e:
            _dbg(f"Auto-color (new notes) failed: {repr(e)}")
        finally:
            try:
                mw.taskman.run_on_main(lambda: mw.progress.finish())
            except Exception:
                pass

    # Kick off background insertion; when done, color only those notes
    def _handle_done(fut):
        try:
            new_nids = fut.result()
        except Exception as e:
            _dbg(f"_insert_and_render failed: {e}")
            mw.taskman.run_on_main(lambda: mw.progress.finish())
            return

        # Now call colorizer with actual list
        mw.taskman.run_on_main(lambda: _apply_color_on_main(new_nids))

    mw.taskman.run_in_background(_insert_and_render, on_done=_handle_done)

def get_api_key():
    config = _get_config()
    key = (config.get("openai_api_key") or "").strip()
    if key:
        return key

    key, ok = QInputDialog.getText(mw, "OpenAI API Key", "Paste your OpenAI API key:")
    if not ok or not key:
        return None

    config["openai_api_key"] = key.strip()
    _save_config(config)
    return key.strip()
def generate_from_pdf():
    # Get API key once
    api_key = get_api_key()
    if not api_key:
        return

    # Pick PDFs
    pdf_paths, _ = QFileDialog.getOpenFileNames(
        mw, "Select PDF(s)", "", "PDF files (*.pdf)"
    )
    if not pdf_paths:
        return

    # Process each selected PDF with its own Options dialog
    for pdf_path in pdf_paths:
        col = mw.col
        deck_name = deck_name_from_pdf_path(pdf_path)
        deck_id = get_or_create_deck(deck_name)
        col.decks.select(deck_id)

        # Page count for this PDF (resets range to 1..last page in the dialog)
        max_pages = _pdf_page_count(pdf_path) or 999

        # Open Options dialog with proper defaults for this file
        dlg = OptionsDialog(mw, max_pages=max_pages)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            continue
        opts = dlg.options()

        if not (opts.get("types_basic") or opts.get("types_cloze")):
            showWarning("Select at least one card type (Basic or Cloze). Aborting.")
            continue

        # Build models (same as before)
        models = {}
        if opts.get("types_basic"):
            models["basic"] = (
                get_basic_model_fallback()
                or ensure_basic_with_slideimage("Basic + Slide")
            )
        if opts.get("types_cloze"):
            models["cloze"] = ensure_cloze_with_slideimage("Cloze + Slide")
        else:
            models["cloze"] = (
                mw.col.models.byName("Cloze + Slide")
                or ensure_cloze_with_slideimage("Cloze + Slide")
            )

        # Background worker completion
        def on_done_wrapper(deck_id=deck_id, deck_name=deck_name):
            def _on_done(fut):
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

        # Launch worker (reuse the same API key; no further prompts)
        mw.taskman.run_in_background(
            lambda p=pdf_path, k=api_key, o=opts: _worker_generate_cards(p, k, o),
            on_done=on_done_wrapper(),
        )

# ---------------------------------------------------------------------------
# Options dialog
# ---------------------------------------------------------------------------
class OptionsDialog(QDialog):
    def __init__(self, parent=None, max_pages=999):
        super().__init__(parent)
        self.setWindowTitle("PDF → Cards: Options")
        self.setModal(True)

        # Load last-used config (we'll only overwrite on OK)
        self.cfg = _get_config().copy()

        v = QVBoxLayout(self)

        # --- Card types ---
        v.addWidget(QLabel("**Card types**"))
        self.chk_basic = QCheckBox("Basic")
        self.chk_cloze = QCheckBox("Cloze (requires OpenAI cloze output)")
        self.chk_basic.setChecked(bool(self.cfg.get("types_basic", True)))
        self.chk_cloze.setChecked(bool(self.cfg.get("types_cloze", False)))
        v.addWidget(self.chk_basic)
        v.addWidget(self.chk_cloze)

        # --- Occlusion ---
        self.chk_occl = QCheckBox("Auto-occlusion near images (AI)")
        self.chk_occl.setChecked(bool(self.cfg.get("occlusion_enabled", True)))
        v.addWidget(self.chk_occl)

        # --- Cards per slide ---
        v.addSpacing(8)
        v.addWidget(QLabel("**Cards per slide**"))
        self.rb_all = QRadioButton("AI decides (all cards returned)")
        self.rb_range = QRadioButton("Range (random subset per slide)")
        mode = self.cfg.get("per_slide_mode", "ai")
        self.rb_all.setChecked(mode == "ai")
        self.rb_range.setChecked(mode == "range")
        self.group_cards = QButtonGroup(self)
        self.group_cards.addButton(self.rb_all)
        self.group_cards.addButton(self.rb_range)
        v.addWidget(self.rb_all)
        v.addWidget(self.rb_range)

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
        v.addLayout(h_per)

        def sync_per():
            enabled = self.rb_range.isChecked()
            self.spin_min.setEnabled(enabled)
            self.spin_max.setEnabled(enabled)
        self.rb_all.toggled.connect(sync_per)
        self.rb_range.toggled.connect(sync_per)
        sync_per()

        # --- Page selection (per PDF; numbers are not persisted) ---
        v.addSpacing(10)
        v.addWidget(QLabel("**Pages (applies to the selected PDF)**"))
        self.rb_pages_all = QRadioButton("All pages (default)")
        self.rb_pages_range = QRadioButton("Page range")
        self.rb_pages_all.setChecked(self.cfg.get("page_mode", "all") == "all")
        self.rb_pages_range.setChecked(self.cfg.get("page_mode", "all") == "range")
        self.group_pages = QButtonGroup(self)
        self.group_pages.addButton(self.rb_pages_all)
        self.group_pages.addButton(self.rb_pages_range)
        v.addWidget(self.rb_pages_all)
        v.addWidget(self.rb_pages_range)

        h_pages = QHBoxLayout()
        self.spin_page_from = QSpinBox()
        self.spin_page_to = QSpinBox()
        self.spin_page_from.setRange(1, max_pages)
        self.spin_page_to.setRange(1, max_pages)

        # Always reset to 1..max_pages for this file
        self.spin_page_from.setValue(1)
        self.spin_page_to.setValue(max_pages)

        h_pages.addWidget(QLabel("From:"))
        h_pages.addWidget(self.spin_page_from)
        h_pages.addWidget(QLabel("To:"))
        h_pages.addWidget(self.spin_page_to)
        v.addLayout(h_pages)

        def sync_pages():
            enabled = self.rb_pages_range.isChecked()
            self.spin_page_from.setEnabled(enabled)
            self.spin_page_to.setEnabled(enabled)
        self.rb_pages_all.toggled.connect(sync_pages)
        self.rb_pages_range.toggled.connect(sync_pages)
        sync_pages()

        # --- Highlight ---
        self.chk_highlight = QCheckBox("Highlight used text on slide")
        self.chk_highlight.setChecked(bool(self.cfg.get("highlight_enabled", True)))
        v.addWidget(self.chk_highlight)

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
        v.addLayout(h_color)
        self.btn_color.clicked.connect(pick_color)

        # --- Coloring (from colorizer) ---
        v.addSpacing(8)
        v.addWidget(QLabel("**Coloring (deck/text highlighting)**"))
        row_col = QHBoxLayout()

        self.btn_color_table = QPushButton("Color Table…")
        self.btn_color_settings = QPushButton("Coloration Settings…")
        row_col.addWidget(self.btn_color_table)
        row_col.addWidget(self.btn_color_settings)
        row_col.addStretch(1)
        v.addLayout(row_col)

        # Wire to colorizer (disable if missing)
        def _connect_colorizer():
            try:
                from .colorizer import on_edit_color_table, open_coloration_settings_dialog
                self.btn_color_table.clicked.connect(on_edit_color_table)
                self.btn_color_settings.clicked.connect(open_coloration_settings_dialog)
            except Exception:
                self.btn_color_table.setEnabled(False)
                self.btn_color_settings.setEnabled(False)
        _connect_colorizer()

        # Post-generation coloring toggles (persist)
        self.chk_color_after = QCheckBox("Color deck after generation")
        self.chk_color_after.setChecked(bool(self.cfg.get("color_after_generation", True)))
        v.addWidget(self.chk_color_after)

        self.chk_ai_extend = QCheckBox("Let AI extend color table before coloring")
        self.chk_ai_extend.setChecked(bool(self.cfg.get("ai_extend_color_table", True)))
        v.addWidget(self.chk_ai_extend)

        # --- Buttons row ---
        btns = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_ok = QPushButton("OK")
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        v.addLayout(btns)
        btn_cancel.clicked.connect(self.reject)
        btn_ok.clicked.connect(self.accept)

    def options(self) -> dict:
        """
        Persist last-used settings (except numeric page range), and return the run options.
        """
        # Save everything except the numeric page range
        c = _get_config()

        # Card types
        c["types_basic"] = self.chk_basic.isChecked()
        c["types_cloze"] = self.chk_cloze.isChecked()

        # Highlight
        c["highlight_enabled"] = self.chk_highlight.isChecked()
        c["highlight_color_hex"] = self.color_hex

        # Occlusion
        c["occlusion_enabled"] = self.chk_occl.isChecked()

        # Cards per slide
        mode = "range" if self.rb_range.isChecked() else "ai"
        c["per_slide_mode"] = mode
        c["per_slide_min"] = int(self.spin_min.value())
        c["per_slide_max"] = int(self.spin_max.value())

        # Page mode only (numeric from/to are not saved)
        c["page_mode"] = "range" if self.rb_pages_range.isChecked() else "all"

        # Coloring prefs
        c["color_after_generation"] = self.chk_color_after.isChecked()
        c["ai_extend_color_table"] = self.chk_ai_extend.isChecked()

        _save_config(c)

        # Return the actual run options (include numeric range for this PDF)
        page_mode = c["page_mode"]
        return {
            "highlight_enabled": c["highlight_enabled"],
            "highlight_color_hex": c["highlight_color_hex"],
            "types_basic": c["types_basic"],
            "types_cloze": c["types_cloze"],
            "per_slide_mode": c["per_slide_mode"],
            "per_slide_min": c["per_slide_min"],
            "per_slide_max": c["per_slide_max"],
            "page_mode": page_mode,
            "page_from": int(self.spin_page_from.value()),
            "page_to": int(self.spin_page_to.value()),
            "occlusion_enabled": c["occlusion_enabled"],
        }

# ---------------------------------------------------------------------------
# Init menu item
# ---------------------------------------------------------------------------

def init_addon():
    action = QAction("Generate Anki cards from PDF", mw)
    action.triggered.connect(generate_from_pdf)
    mw.form.menuTools.addAction(action)
