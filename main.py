
# main.py
# PDF → OpenAI (background) → Notes; image rendered by template as:
#
#   BASIC (Back):
#     {{FrontSide}}
#
#     <hr id=answer>
#
#     {{Back}}
#
#     <br><br>
#
#     <img src={{SlideImage}}>
#
#   CLOZE (Back):
#     {{cloze:Text}}
#
#     {{Back Extra}}
#
#     <img src={{SlideImage}}>
#
# Features:
#   • Per–slide card count: "AI (all)" OR "Range (min–max; random n per slide)"
#   • Card types: Basic and/or Cloze (checkboxes). If neither checked, aborts.
#   • Slide image rendered by template via SlideImage field (exact formats above).
#   • No “generated X cards” / image summary popups (progress + warnings only).
#
# Compatible with Anki 2.1.x / Python 3.9. Pillow is OPTIONAL.

import os
import re
import sys
import random
import traceback
from io import BytesIO
from typing import List, Dict, Optional, Tuple

from aqt import mw
from aqt.utils import showWarning
from aqt.qt import (
    QAction, QFileDialog, QInputDialog, QMessageBox,
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox,
    QRadioButton, QSpinBox, QPushButton
)


# --- at top of main.py ---
import os, re, time
from aqt import mw

def _dbg(msg: str) -> None:
    """Append a timestamped line to pdf2cards_debug.log in the user profile folder."""
    try:
        path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        # stay silent in production
        pass


# --- Optional: Pillow ---
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    Image = None  # type: ignore
    HAVE_PIL = False

# --- Qt PDF (optional) ---
try:
    from PyQt6.QtGui import QImage, QPainter
    from PyQt6.QtCore import QRectF, QBuffer, QByteArray, QIODevice
    try:
        from PyQt6.QtPdf import QPdfDocument
        HAVE_QTPDF = True
    except Exception:
        HAVE_QTPDF = False
except Exception:
    HAVE_QTPDF = False

from .pdf_parser import extract_text_from_pdf
from .openai_cards import generate_cards

ADDON_ID = os.path.basename(os.path.dirname(__file__))

# -------------------------------
# Config helpers
# -------------------------------
def _get_config() -> dict:
    c = mw.addonManager.getConfig(ADDON_ID) or {}
    c.setdefault("openai_api_key", "")
    c.setdefault("types_basic", True)
    c.setdefault("types_cloze", False)
    c.setdefault("per_slide_mode", "ai")  # "ai" or "range"
    c.setdefault("per_slide_min", 1)
    c.setdefault("per_slide_max", 3)
    return c

def _save_config(c: dict) -> None:
    mw.addonManager.writeConfig(ADDON_ID, c)

# -------------------------------
# Deck helpers
# -------------------------------
def deck_name_from_pdf_path(pdf_path: str) -> str:
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return base.strip()


def get_or_create_deck(deck_name: str) -> int:
    col = mw.col
    did = col.decks.id(deck_name, create=False)
    if did:
        return did
    return col.decks.id(deck_name, create=True)

# -------------------------------
# Notetypes (build fresh; do NOT copy+add)
# -------------------------------
def ensure_basic_with_slideimage(model_name: str = "Basic + Slide") -> dict:
    """
    Ensure a Basic-like notetype whose Back template is EXACTLY:

      {{FrontSide}}

      <hr id=answer>

      {{Back}}

      <br><br>

      <img src={{SlideImage}}>
    """
    col = mw.col
    m = col.models.byName(model_name)

    if not m:
        # Fresh Basic-like notetype
        m = col.models.new(model_name)     # id==0 (safe to add)
        f_front = col.models.newField("Front")
        f_back  = col.models.newField("Back")
        col.models.addField(m, f_front)
        col.models.addField(m, f_back)
        t = col.models.newTemplate("Card 1")
        t["qfmt"] = "{{Front}}"
        t["afmt"] = (
            "{{FrontSide}}\n\n"
            "<hr id=answer>\n\n"
            "{{Back}}\n\n"
            "<br><br>\n\n"
            "<img src={{SlideImage}}>"
        )
        col.models.addTemplate(m, t)
        col.models.add(m)

    # Ensure SlideImage field
    names = [f.get("name") for f in (m.get("flds") or [])]
    if "SlideImage" not in names:
        fld = col.models.newField("SlideImage")
        try:
            idx_back = names.index("Back")
            m["flds"] = m["flds"][:idx_back] + [fld] + m["flds"][idx_back:]
        except ValueError:
            col.models.addField(m, fld)

    # Force EXACT Back template string (prevents duplicates)
    desired_afmt = (
        "{{FrontSide}}\n\n"
        "<hr id=answer>\n\n"
        "{{Back}}\n\n"
        "<br><br>\n\n"
        "<img src={{SlideImage}}>"
    )
    changed = False
    for t in (m.get("tmpls") or []):
        if t.get("afmt", "") != desired_afmt:
            t["afmt"] = desired_afmt
            changed = True
    if changed:
        col.models.save(m)

    return m


def get_basic_model_fallback():
    col = mw.col
    m = col.models.byName("Basic")
    if m:
        return m
    for cand in col.models.all():
        if len(cand.get("flds", [])) >= 2 and len(cand.get("tmpls", [])) >= 1:
            return cand
    return col.models.current()

# -------------------------------
# API key
# -------------------------------
def get_api_key():
    config = _get_config()
    api_key = (config.get("openai_api_key") or "").strip()
    if api_key:
        return api_key
    api_key, ok = QInputDialog.getText(
        mw,
        "OpenAI API Key",
        "Paste your OpenAI API key:"
    )
    if not ok or not api_key:
        return None
    config["openai_api_key"] = api_key.strip()
    _save_config(config)
    return api_key.strip()




import re
from typing import Tuple


def ensure_cloze_with_slideimage(model_name: str = "Cloze + Slide") -> dict:
    """
    Ensure a Cloze-style notetype whose Back template is EXACTLY:

      {{cloze:Text}}

      {{Back Extra}}

      <img src={{SlideImage}}>
    """
    col = mw.col
    m = col.models.byName(model_name)

    if not m:
        # Fresh Cloze notetype (id==0 → safe to add)
        m = col.models.new(model_name)
        m["type"] = 1  # CLOZE

        # Fields
        f_text = col.models.newField("Text")
        f_back = col.models.newField("Back Extra")
        col.models.addField(m, f_text)
        col.models.addField(m, f_back)

        # Template (EXACT)
        t = col.models.newTemplate("Cloze")
        t["qfmt"] = "{{cloze:Text}}"
        t["afmt"] = (
            "{{cloze:Text}}\n\n"
            "{{Back Extra}}\n\n"
            "<img src={{SlideImage}}>"
        )
        col.models.addTemplate(m, t)
        col.models.add(m)

    # Ensure SlideImage field exists
    names = [f.get("name") for f in (m.get("flds") or [])]
    if "SlideImage" not in names:
        fld = col.models.newField("SlideImage")
        col.models.addField(m, fld)

    # Force EXACT front/back templates
    desired_qfmt = "{{cloze:Text}}"
    desired_afmt = (
        "{{cloze:Text}}\n\n"
        "{{Back Extra}}\n\n"
        "<img src={{SlideImage}}>"
    )
    changed = False
    for t in (m.get("tmpls") or []):
        if t.get("qfmt", "") != desired_qfmt:
            t["qfmt"] = desired_qfmt
            changed = True
        if t.get("afmt", "") != desired_afmt:
            t["afmt"] = desired_afmt
            changed = True
    if changed:
        col.models.save(m)

    return m



# -------------------------------
# Card move
# -------------------------------
def force_move_cards_to_deck(cids: list, deck_id: int) -> None:
    if not cids:
        return
    col = mw.col
    try:
        col.decks.set_card_deck(cids, deck_id)
    except Exception:
        try:
            col.decks.setDeck(cids, deck_id)  # type: ignore[attr-defined]
        except Exception:
            pass

# ======================================================
# Enable vendored PyMuPDF if present
# ======================================================
def _enable_local_pymupdf() -> None:
    base = os.path.join(os.path.dirname(__file__), "_vendor", "pymupdf")
    pkg_dir = os.path.join(base, "pymupdf")
    fitz_dir = os.path.join(base, "fitz")
    if os.path.isdir(base) and base not in sys.path:
        sys.path.insert(0, base)
    for d in (
        os.path.join(base, "PyMuPDF.libs"),
        os.path.join(base, "pymupdf.libs"),
        pkg_dir, fitz_dir,
    ):
        if os.path.isdir(d):
            try:
                os.add_dll_directory(d)  # Py 3.8+
            except Exception:
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

# ======================================================
# Image helpers (PyMuPDF → QtPdf → pypdf)
# ======================================================
def _sniff_image_ext(data: bytes) -> str:
    if len(data) >= 8 and data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if len(data) >= 12 and data[:12] in (b"\x00\x00\x00\x0cjP  \r\n\x87\n",):
        return "jp2"
    if len(data) >= 6 and data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return "bin"

def _resize_to_max_width(png_bytes: bytes, max_width: int = 1600) -> bytes:
    if not HAVE_PIL:
        return png_bytes
    try:
        with Image.open(BytesIO(png_bytes)) as im:  # type: ignore[attr-defined]
            if im.width <= max_width:
                return png_bytes
            new_h = int(im.height * max_width / im.width)
            im = im.convert("RGB").resize((max_width, new_h), Image.LANCZOS)
            out = BytesIO()
            im.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:
        return png_bytes

def _render_with_pymupdf(pdf_path: str, page_number: int, dpi: int = 200):
    try:
        _enable_local_pymupdf()
        import pymupdf
        doc = pymupdf.open(pdf_path)
        page = doc[page_number - 1]
        zoom = dpi / 72.0
        mat = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    except Exception:
        return None

def _render_with_qtpdf(pdf_path: str, page_number: int, dpi: int = 200) -> Optional[bytes]:
    if not HAVE_QTPDF:
        return None
    try:
        doc = QPdfDocument()
        doc.load(pdf_path)
        if doc.status() != QPdfDocument.Status.Ready:
            return None
        if page_number < 1 or page_number > doc.pageCount():
            return None
        page_idx = page_number - 1
        page_size_points = doc.pagePointSize(page_idx)
        if not page_size_points.isValid():
            return None
        width_px  = max(10, int(page_size_points.width()  * dpi / 72.0))
        height_px = max(10, int(page_size_points.height() * dpi / 72.0))
        img = QImage(width_px, height_px, QImage.Format.Format_RGB32)
        img.fill(0xFFFFFFFF)
        painter = QPainter(img)
        try:
            doc.render(page_idx, painter, QRectF(0, 0, width_px, height_px), QPdfDocument.RenderFlags())
        finally:
            painter.end()
        if HAVE_PIL:
            buffer = img.bits().asstring(img.width() * img.height() * 4)
            pim = Image.frombytes("RGBA", (img.width(), img.height()), buffer, "raw", "BGRA")
            pim = pim.convert("RGB")
            out = BytesIO()
            pim.save(out, format="PNG", optimize=True)
            return out.getvalue()
        else:
            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            img.save(buf, "PNG")
            buf.close()
            return bytes(ba)
    except Exception:
        return None

def _extract_largest_embedded_image(pdf_path: str, page_number: int) -> Optional[bytes]:
    try:
        from pypdf import PdfReader
    except Exception:
        return None
    try:
        reader = PdfReader(pdf_path)
        page = reader.pages[page_number - 1]
        images = getattr(page, "images", None)
        candidates = []
        if images:
            for img in images:
                data = getattr(img, "data", None)
                w = getattr(img, "width", 0) or 0
                h = getattr(img, "height", 0) or 0
                if isinstance(data, (bytes, bytearray)) and w and h:
                    candidates.append((w * h, bytes(data)))
        if not candidates:
            return None
        _, best = max(candidates, key=lambda x: x[0])
        if not HAVE_PIL:
            return best
        try:
            with Image.open(BytesIO(best)) as im:  # type: ignore[attr-defined]
                im = im.convert("RGB")
                out = BytesIO()
                im.save(out, format="PNG", optimize=True)
                return out.getvalue()
        except Exception:
            return best
    except Exception:
        return None

def render_page_blob(pdf_path: str, page_number: int, dpi: int = 200, max_width: int = 1600) -> Optional[Tuple[bytes, str]]:
    png = _render_with_pymupdf(pdf_path, page_number, dpi=dpi)
    if png:
        png = _resize_to_max_width(png, max_width=max_width)
        return png, "png"
    png2 = _render_with_qtpdf(pdf_path, page_number, dpi=dpi)
    if png2:
        png2 = _resize_to_max_width(png2, max_width=max_width)
        return png2, "png"
    blob = _extract_largest_embedded_image(pdf_path, page_number)
    if not blob:
        return None
    if HAVE_PIL:
        ext = _sniff_image_ext(blob)
        if ext != "png":
            return blob, ext
        return blob, "png"
    ext = _sniff_image_ext(blob)
    return blob, ext

def _write_media_file(basename: str, data: bytes) -> Optional[str]:
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

# -------------------------------
# Options dialog (types + per-slide count control)
# -------------------------------
class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PDF → Cards: Options")
        self.setModal(True)
        self.cfg = _get_config()

        v = QVBoxLayout(self)

        # Card types
        v.addWidget(QLabel("<b>Card types</b>"))
        self.chk_basic = QCheckBox("Basic")
        self.chk_cloze = QCheckBox("Cloze (only if AI returns cloze markup)")
        self.chk_basic.setChecked(bool(self.cfg.get("types_basic", True)))
        self.chk_cloze.setChecked(bool(self.cfg.get("types_cloze", False)))
        v.addWidget(self.chk_basic)
        v.addWidget(self.chk_cloze)

        # Per-slide count
        v.addSpacing(8)
        v.addWidget(QLabel("<b>Cards per slide</b>"))
        self.rb_all = QRadioButton("AI decides (use all returned)")
        self.rb_range = QRadioButton("Range (random n per slide)")
        mode = self.cfg.get("per_slide_mode", "ai")
        self.rb_all.setChecked(mode == "ai")
        self.rb_range.setChecked(mode == "range")
        v.addWidget(self.rb_all)
        v.addWidget(self.rb_range)

        h = QHBoxLayout()
        self.spin_min = QSpinBox()
        self.spin_max = QSpinBox()
        self.spin_min.setRange(1, 50)
        self.spin_max.setRange(1, 50)
        self.spin_min.setValue(int(self.cfg.get("per_slide_min", 1)))
        self.spin_max.setValue(int(self.cfg.get("per_slide_max", 3)))
        h.addWidget(QLabel("Min:"))
        h.addWidget(self.spin_min)
        h.addWidget(QLabel("Max:"))
        h.addWidget(self.spin_max)
        v.addLayout(h)

        # Buttons
        btns = QHBoxLayout()
        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)

        # enable/disable min/max by mode
        self.rb_all.toggled.connect(self._sync_enabled)
        self.rb_range.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    def _sync_enabled(self):
        enabled = self.rb_range.isChecked()
        self.spin_min.setEnabled(enabled)
        self.spin_max.setEnabled(enabled)

    def options(self) -> dict:
        mode = "range" if self.rb_range.isChecked() else "ai"
        minv = int(self.spin_min.value())
        maxv = int(self.spin_max.value())
        if maxv < minv:
            minv, maxv = maxv, minv  # swap silently

        # Save back to config
        c = _get_config()
        c["types_basic"] = self.chk_basic.isChecked()
        c["types_cloze"] = self.chk_cloze.isChecked()
        c["per_slide_mode"] = mode
        c["per_slide_min"] = minv
        c["per_slide_max"] = maxv
        _save_config(c)

        return {
            "types_basic": self.chk_basic.isChecked(),
            "types_cloze": self.chk_cloze.isChecked(),
            "per_slide_mode": mode,
            "per_slide_min": minv,
            "per_slide_max": maxv,
        }






import re
from typing import Tuple





# -------------------------------
# Background worker (many-cards mode + range control)
# -------------------------------
def _worker_generate_cards(pdf_path: str, api_key: str, opts: dict) -> Dict:
    """
    MANY-CARDS MODE (per page) with per-slide control.
    - Extract text per page
    - Call LLM for each page
    - Keep all, or a random # in [min, max] per slide (prefer Cloze first, then Basic)
    - Return (front, back, page_no) tuples without filtering by type.
    """
    def ui_update(label: str):
        try:
            mw.progress.update(label=label)
        except Exception:
            pass

    try:
        pages = extract_text_from_pdf(pdf_path)
        total_pages = len(pages)
        results: List[Tuple[str, str, int]] = []
        page_errors: List[str] = []

        if not pages:
            return {"ok": True, "cards": [], "pages": 0, "errors": [], "meta": {"pdf_path": pdf_path}}

        mode = opts.get("per_slide_mode", "ai")
        minv = int(opts.get("per_slide_min", 1))
        maxv = int(opts.get("per_slide_max", 3))
        if maxv < minv:
            minv, maxv = maxv, minv

        
        for idx, page in enumerate(pages, start=1):
            mw.taskman.run_on_main(
                lambda i=idx, t=total_pages: ui_update(f"Processing page {i} of {t}")
            )

            try:
                cards = []


                # Always generate Basic if Cloze is enabled
                need_basic = opts.get("types_basic") or opts.get("types_cloze")

                if need_basic:
                    out_basic = generate_cards(
                        page["text"],
                        api_key,
                        mode="basic"
                    )
                    _dbg(f"BASIC RAW CARDS (page {page['page']}): {out_basic.get('cards')}")
                    cards += out_basic.get("cards", [])



                if opts.get("types_cloze"):
                    out_cloze = generate_cards(
                        page["text"],
                        api_key,
                        mode="cloze"
                    )
                    _dbg(f"CLOZE RAW OUTPUT: {out_cloze}")
                    _dbg(f"CLOZE RAW OUTPUT: {out_cloze.get('cards')}")
                    _dbg(f"CLOZE RAW CARDS (page {page['page']}): {out_cloze.get('cards')}")
                    cards += out_cloze.get("cards", [])


            except Exception as e:
                page_errors.append(f"page {page['page']}: {e}")
                continue

            # ✅ cards is now final for this page
            # ✅ NO api_out anywhere below this point

            if mode == "range":
                if cards:
                    n = max(0, min(random.randint(minv, maxv), len(cards)))

                    # Put CLOZE cards first, then BASIC
                    cloze_first = []
                    non_cloze   = []
                    for c in cards:
                        f = (c.get("front") or "")
                        b = (c.get("back")  or "")
                        if "{{c" in (f + b):
                            cloze_first.append(c)
                        else:
                            non_cloze.append(c)

                    cards = (cloze_first + non_cloze)[:n]
                else:
                    cards = []


            for card in cards:

                front = (card.get("front", "") or "").strip()
                back  = (card.get("back",  "") or "").strip()

                # keep BASIC (front & back) or any card that actually has cloze markup
                if (front and back) or ("{{c" in front) or ("{{c" in back):
                    results.append((front, back, page["page"]))


        return {"ok": True, "cards": results, "pages": total_pages, "errors": page_errors, "meta": {"pdf_path": pdf_path}}

    except Exception as e:
        tb = traceback.format_exc()
        return {"ok": False, "cards": [], "pages": 0, "errors": [], "error": str(e), "traceback": tb}

# -------------------------------
# After worker completes: insert notes and embed slide images
# -------------------------------

def _on_worker_done(result: Dict, deck_id: int, deck_name: str, models: Dict[str, dict], opts: dict) -> None:
    mw.progress.finish()

    # Validate result
    if not isinstance(result, dict) or not result.get("ok", False):
        tb = ""
        if isinstance(result, dict):
            tb = result.get("traceback", "")
        tb_snippet = (tb[:1200] + "\n…(truncated)…") if tb and len(tb) > 1200 else tb
        showWarning(
            "Generation failed.\n\n"
            f"Error: { (result.get('error') if isinstance(result, dict) else 'unknown') }\n\n"
            f"{tb_snippet}"
        )
        return

    cards: List[Tuple[str, str, int]] = result.get("cards", [])
    meta = result.get("meta", {}) if isinstance(result.get("meta"), dict) else {}
    pdf_path = meta.get("pdf_path", None)
    if not cards:
        return  # silent finish

    # ---- Render one image per unique page ----
    page_to_media: Dict[int, Optional[str]] = {}
    if pdf_path:
        unique_pages = sorted({p for _, _, p in cards})
        mw.progress.start(label=f"Preparing slide images (0/{len(unique_pages)})…", immediate=True)
        try:
            for i, p in enumerate(unique_pages, start=1):
                
                try:
                    res = render_page_blob(pdf_path, p, dpi=200, max_width=1600)
                    if res:
                        data, ext = res
                        base = os.path.splitext(os.path.basename(pdf_path))[0]
                        suggested = f"{base}_p{p}.{ext if ext in ('png','jpg','jp2','gif') else 'bin'}"
                        stored = _write_media_file(suggested, data)
                        page_to_media[p] = stored
                    else:
                        page_to_media[p] = None
                except Exception:
                    page_to_media[p] = None
                if i % 2 == 0 or i == len(unique_pages):
                    mw.progress.update(label=f"Preparing slide images ({i}/{len(unique_pages)})…")
        finally:
            mw.progress.finish()

    # ---- Insert notes (Basic and/or Cloze) ----
    col = mw.col
    col.decks.select(deck_id)

    want_basic = bool(opts.get("types_basic", True))
    want_cloze = bool(opts.get("types_cloze", False))
    if not (want_basic or want_cloze):
        showWarning("No card types selected. Aborting.")
        return

    # Ensure models exist and have the EXACT templates you want
    if want_basic:
        models["basic"] = ensure_basic_with_slideimage(models["basic"].get("name", "Basic + Slide"))
    if want_cloze:
        models["cloze"] = ensure_cloze_with_slideimage(models["cloze"].get("name", "Cloze + Slide"))


    mw.progress.start(label=f"Inserting {len(cards)} card(s)…", immediate=True)
    try:
        from os.path import basename

        
        for idx, (front, back, page_no) in enumerate(cards, start=1):
            media_name = page_to_media.get(page_no)
            fname = basename(media_name) if media_name else ""

            # --- log the inputs ---

            is_cloze_front = "{{c" in front
            is_cloze_back  = "{{c" in back



            
            _dbg(
                f"card#{idx} page={page_no} "
                f"is_cloze_front={is_cloze_front} is_cloze_back={is_cloze_back} "
                f"front='{(front or '')[:80]}' back='{(back or '')[:80]}'"
            )


                


            is_cloze = is_cloze_front or is_cloze_back
            if is_cloze and want_cloze:
                model = models["cloze"]
                col.models.set_current(model)
                note = col.newNote()
                note.did = deck_id

                # Prefer non-question cloze text

                
                if is_cloze_front:
                    text_val, back_extra = front, back
                else:
                    text_val, back_extra = back, front

                # guard: don’t add a cloze note with empty Text
                if not text_val.strip():
                    _dbg("SKIP: empty cloze Text")
                    continue

                _dbg(f"CLOZE CHOSEN text_len={len(text_val)} back_extra_len={len(back_extra)} preview='{text_val[:120]}'")


                note["Text"] = text_val
                note["Back Extra"] = back_extra
                if "SlideImage" in note:
                    note["SlideImage"] = fname

                note.tags.append("pdf2cards:ai_cloze")
                col.addNote(note)
                
                cids = [c.id for c in note.cards()]
                force_move_cards_to_deck(cids, deck_id)
                _dbg(f"card#{idx} -> CLOZE created={len(note.cards())} moved_to={deck_id}")

                continue






            elif want_basic:
                # Basic note (strip cloze markup, just in case)
                model = models["basic"]
                col.models.set_current(model)
                note = col.newNote()
                note.did = deck_id
                note["Front"] = front
                note["Back"]  = back
                if "SlideImage" in note:
                    note["SlideImage"] = fname
                note.tags.append("pdf2cards:basic")
                col.addNote(note)
                _dbg(f"card#{idx} -> BASIC")
                try:
                    cids = [c.id for c in note.cards()]
                    force_move_cards_to_deck(cids, deck_id)
                except Exception:
                    pass



            if idx % 5 == 0:
                mw.progress.update(label=f"Inserting {idx}/{len(cards)}…")
    finally:
        mw.progress.finish()
        col.save()
    # Silent finish—no final popups.

# -------------------------------
# Main entry (spawns background task)
# -------------------------------
def generate_from_pdf():
    api_key = get_api_key()
    if not api_key:
        return

    pdf_path, _ = QFileDialog.getOpenFileName(
        mw, "Select PDF", "", "PDF files (*.pdf)"
    )
    if not pdf_path:
        return

    # Options dialog
    dlg = OptionsDialog(mw)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return
    opts = dlg.options()
    if not (opts.get("types_basic") or opts.get("types_cloze")):
        showWarning("Select at least one card type (Basic or Cloze). Aborting.")
        return

    # Deck + models
    col = mw.col
    deck_name = deck_name_from_pdf_path(pdf_path)
    deck_id = get_or_create_deck(deck_name)
    col.decks.select(deck_id)

    # Prepare models—with EXACT templates
    models: Dict[str, dict] = {}
    if opts.get("types_basic"):
        models["basic"] = ensure_basic_with_slideimage("Basic + Slide")
    else:
        models["basic"] = get_basic_model_fallback() or ensure_basic_with_slideimage("Basic + Slide")
    if opts.get("types_cloze"):
        models["cloze"] = ensure_cloze_with_slideimage("Cloze + Slide")
    else:
        models["cloze"] = mw.col.models.byName("Cloze + Slide") or ensure_cloze_with_slideimage("Cloze + Slide")

    mw.progress.start(label="Generating Anki cards from PDF…", immediate=True)

    def _on_done(fut):
        try:
            res = fut.result()
        except Exception as e:
            tb = traceback.format_exc()
            tb_snippet = (tb[:1200] + "\n…(truncated)…") if len(tb) > 1200 else tb
            mw.progress.finish()
            showWarning(f"Generation failed.\n\nError: {e}\n\n{tb_snippet}")
            return
        _on_worker_done(res, deck_id, deck_name, models, opts)

    mw.taskman.run_in_background(
        lambda: _worker_generate_cards(pdf_path, api_key, opts),
        on_done=_on_done,
    )

# -------------------------------
# Diagnostics (optional)
# -------------------------------
def check_pdf_renderer():
    import importlib
    msgs = []

    base = os.path.join(os.path.dirname(__file__), "_vendor", "pymupdf")
    msgs.append(f"Vendor base: {base}  exists={os.path.isdir(base)}")
    if os.path.isdir(base) and base not in sys.path:
        sys.path.insert(0, base)
        msgs.append("Added vendor base to sys.path")

    cands = []
    for name in ("PyMuPDF.libs", "pymupdf.libs", "pymupdf", "fitz"):
        d = os.path.join(base, name)
        if os.path.isdir(d):
            cands.append(d)
    for d in cands:
        try:
            os.add_dll_directory(d)
            msgs.append(f"DLL dir added: {d}")
        except Exception:
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

    loaded = None
    try:
        loaded = importlib.import_module("pymupdf")
        msgs.append(f"Imported pymupdf ✅ version={getattr(loaded,'__version__',None)} file={getattr(loaded,'__file__',None)}")
    except Exception as e1:
        msgs.append(f"pymupdf import failed: {repr(e1)}")
        try:
            loaded = importlib.import_module("fitz")
            msgs.append(f"Imported fitz ✅ version={getattr(loaded,'__version__',None)} file={getattr(loaded,'__file__',None)}")
        except Exception as e2:
            msgs.append(f"fitz import failed: {repr(e2)}")

    QMessageBox.information(mw, "PDF Renderer Check", "\n".join(msgs))

def get_basic_model():
    col = mw.col
    m = col.models.byName("Basic")
    if m:
        return m
    for cand in col.models.all():
        if len(cand.get("flds", [])) >= 2 and len(cand.get("tmpls", [])) >= 1:
            return cand
    return col.models.current()

def init_addon():
    action = QAction("Generate Anki cards from PDF", mw)
    action.triggered.connect(generate_from_pdf)
    mw.form.menuTools.addAction(action)

    diag = QAction("Check PDF Renderer (automatic-anki)", mw)
    diag.triggered.connect(check_pdf_renderer)
    mw.form.menuTools.addAction(diag)
