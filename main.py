
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

from aqt.qt import QColorDialog

import os
import re
import sys
import random
import traceback
from io import BytesIO
from typing import List, Dict, Optional, Tuple

from aqt import mw
from .pdf_parser import extract_words_with_boxes, boxes_for_phrase, sentence_rects_for_phrase
from aqt.utils import showWarning

from .pdf_images import render_page_as_png   # or render_page_blob
from .pdf_images import render_page_as_png_with_highlights


# Near-image occlusion imports
from .pdf_parser import extract_image_boxes  # image block finder
from .openai_cards import suggest_occlusions_from_image  # vision masks
from PIL import Image, ImageDraw  # ensured available by your optional Pillow

from aqt.qt import (
    QAction, QFileDialog, QInputDialog, QMessageBox, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QRadioButton,
    QSpinBox, QPushButton, QButtonGroup  # ← add this
)


# --- Auto-occlusion (AI) constants (no UI) --------------------------------
_OCCLUSION_ENABLED = True
_OCCLUSION_DPI = 200                # render scale for crops
_IMAGE_MARGIN_PT = 36.0             # ~0.5 inch around image (PDF points)
_MAX_MASKS_PER_CROP = 12            # safety cap


def _write_media_file(basename: str, data: bytes) -> Optional[str]:
    """
    Store bytes into Anki media. Returns the stored filename, or None on failure.
    """
    try:
        return mw.col.media.write_data(basename, data)
    except Exception:
        try:
            import tempfile, os
            tmp = os.path.join(tempfile.gettempdir(), basename)
            with open(tmp, "wb") as f:
                f.write(data)
            return mw.col.media.add_file(tmp)
        except Exception:
            return None

def _render_page_png_no_resize(pdf_path: str, page_number: int, dpi: int = 200) -> bytes:
    """
    Render the page at 'dpi' with PyMuPDF directly and return PNG bytes.
    We avoid any max_width downscale so math from PDF points stays linear.
    """
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        return pix.tobytes("png")
    except Exception:
        return b""


def _crop_png_region(png_bytes: bytes, rect_pt: dict, dpi: int) -> bytes:
    """Crop the PNG to the rectangle (in PDF points)."""
    if not png_bytes:
        return b""
    scale = dpi / 72.0
    x = int(max(0, rect_pt["x"] * scale)); y = int(max(0, rect_pt["y"] * scale))
    w = int(max(1, rect_pt["w"] * scale)); h = int(max(1, rect_pt["h"] * scale))
    with Image.open(BytesIO(png_bytes)) as im:
        x2 = min(im.width, x + w); y2 = min(im.height, y + h)
        x1 = min(max(0, x), im.width); y1 = min(max(0, y), im.height)
        if x2 <= x1 or y2 <= y1:
            return b""
        crop = im.crop((x1, y1, x2, y2)).convert("RGB")
        out = BytesIO(); crop.save(out, format="PNG", optimize=True)
        return out.getvalue()


def _mask_one_rect_on_png(png_bytes: bytes, rect_px: dict,
                          fill=(242, 242, 242), outline=(160, 160, 160)) -> bytes:
    """Return a PNG with a single mask rectangle painted."""
    if not png_bytes:
        return b""
    with Image.open(BytesIO(png_bytes)) as im:
        im = im.convert("RGB")
        drw = ImageDraw.Draw(im)
        x = int(rect_px.get("x", 0)); y = int(rect_px.get("y", 0))
        w = int(rect_px.get("w", 0)); h = int(rect_px.get("h", 0))
        if w > 0 and h > 0:
            drw.rectangle([x, y, x + w, y + h], fill=fill, outline=outline, width=2)
        out = BytesIO(); im.save(out, format="PNG", optimize=True)
        return out.getvalue()

# --- debug log ---------------------------------------------------------------
import time
def _dbg(msg: str) -> None:
    """Append a timestamped line to pdf2cards_debug.log in the user profile folder."""
    try:
        path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        pass

# --- Optional: Pillow --------------------------------------------------------
try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    Image = None  # type: ignore
    HAVE_PIL = False

# --- Qt PDF (optional) -------------------------------------------------------
try:
    from PyQt6.QtGui import QImage, QPainter, QColor
    from PyQt6.QtCore import QRectF, QBuffer, QByteArray, QIODevice, QRect, Qt
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

    c.setdefault("highlight_span_sentences", 1)
    
    c.setdefault("highlight_enabled", True)          # ✅ new
    c.setdefault("highlight_color_hex", "#FF69B4")   # ✅ new 
    c.setdefault("occlusion_enabled", True)
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

def _rgba_from_hex(hex_str: str, alpha: int = 55):
    """
    Convert "#RRGGBB" to (R,G,B,A). Alpha default is ~22% opacity (55/255).
    """
    s = (hex_str or "").strip()
    if not s.startswith("#") or len(s) != 7:
        s = "#FF69B4"  # fallback pink
    r = int(s[1:3], 16); g = int(s[3:5], 16); b = int(s[5:7], 16)
    return (r, g, b, int(alpha))

# -------------------------------
# -------------------------------
# Ensure Basic + Slide model
# -------------------------------
def ensure_basic_with_slideimage(model_name: str = "Basic + Slide") -> dict:
    """
    Ensure a Basic-like notetype.
    Back template ALWAYS shows:
      - the question ({{FrontSide}})
      - a divider
      - the answer
      - the slide image
    This function intentionally FIXES existing broken templates.
    """
    col = mw.col
    m = col.models.byName(model_name)
    created = False

    if not m:
        m = col.models.new(model_name)
        created = True

    # -------------------------------
    # Fields
    # -------------------------------
    want_fields = ["Front", "Back", "SlideImage"]
    have_names = [f.get("name") for f in (m.get("flds") or [])]

    for name in want_fields:
        if name not in have_names:
            fld = col.models.newField(name)
            col.models.addField(m, fld)

    # -------------------------------
    # Template (FORCE FIX)
    # -------------------------------
    qfmt = "{{Front}}"
    afmt = (
        "{{FrontSide}}\n\n"
        "<hr id=answer>\n"
        "{{Back}}\n\n"
        "<br><br>"
        "{{SlideImage}}"
    )

    tmpls = m.get("tmpls") or []
    if not tmpls:
        t = col.models.newTemplate("Card 1")
        t["qfmt"] = qfmt
        t["afmt"] = afmt
        col.models.addTemplate(m, t)
    else:
        # FORCE overwrite to fix old broken templates
        t = tmpls[0]
        t["qfmt"] = qfmt
        t["afmt"] = afmt
        m["tmpls"][0] = t

    # -------------------------------
    # CSS (light, safe default)
    # -------------------------------
    css = """
.card {
    font-family: arial;
    font-size: 20px;
    text-align: center;
    color: black;
    background-color: white;
}
.card img {
    max-width: 100%;
    height: auto;
}
""".strip()

    m["css"] = css

    # -------------------------------
    # Save
    # -------------------------------
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




def ensure_cloze_with_slideimage(model_name: str = "Cloze + Slide") -> dict:
    col = mw.col
    m = col.models.byName(model_name)
    if not m:
        m = col.models.new(model_name)
        m["type"] = 1  # CLOZE
        f_text = col.models.newField("Text")
        f_back = col.models.newField("Back Extra")
        col.models.addField(m, f_text)
        col.models.addField(m, f_back)

        t = col.models.newTemplate("Cloze")
        # Front shows the clozed text (with blanks)
        t["qfmt"] = "{{cloze:Text}}"
        # Back reveals the cloze, shows Back Extra and the slide image
        t["afmt"] = (
            "{{cloze:Text}}\n\n"
            "{{#Back Extra}}{{Back Extra}}{{/Back Extra}}\n\n"
            "<br><br>"
            "{{SlideImage}}"
        )
        col.models.addTemplate(m, t)
        col.models.add(m)

    # Ensure SlideImage field exists
    names = [f.get("name") for f in (m.get("flds") or [])]
    if "SlideImage" not in names:
        fld = col.models.newField("SlideImage")
        col.models.addField(m, fld)

    # Only fill missing templates; do not overwrite existing ones
    tmpls = m.get("tmpls") or []
    if not tmpls:
        t = col.models.newTemplate("Cloze")
        t["qfmt"] = "{{cloze:Text}}"
        t["afmt"] = (
            "{{cloze:Text}}\n\n"
            "{{#Back Extra}}{{Back Extra}}{{/Back Extra}}\n\n"
            "<br><br>"
            "{{SlideImage}}"
        )
        col.models.addTemplate(m, t)
    else:
        t = tmpls[0]
        if not (t.get("qfmt") or "").strip():
            t["qfmt"] = "{{cloze:Text}}"
        if not (t.get("afmt") or "").strip():
            t["afmt"] = (
                "{{cloze:Text}}\n\n"
                "{{#Back Extra}}{{Back Extra}}{{/Back Extra}}\n\n"
                "<br><br>"
                "{{SlideImage}}"
            )

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

# -------------------------------
# Options dialog (types + per-slide count + page selection)
# -------------------------------
class OptionsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PDF → Cards: Options")
        self.setModal(True)
        self.cfg = _get_config()

        v = QVBoxLayout(self)

        # -----------------------
        # Card types
        # -----------------------
        v.addWidget(QLabel("<b>Card types</b>"))
        self.chk_basic = QCheckBox("Basic")
        self.chk_cloze = QCheckBox("Cloze (only if AI returns cloze markup)")
        self.chk_basic.setChecked(bool(self.cfg.get("types_basic", True)))
        self.chk_cloze.setChecked(bool(self.cfg.get("types_cloze", False)))
        v.addWidget(self.chk_basic)
        v.addWidget(self.chk_cloze)

        # --- Auto-occlusion toggle ---
        self.chk_occl = QCheckBox("Auto-occlusion near images (AI)")
        self.chk_occl.setChecked(bool(self.cfg.get("occlusion_enabled", True)))
        v.addWidget(self.chk_occl)

        # -----------------------
        # Cards per slide
        # -----------------------
        v.addSpacing(8)
        v.addWidget(QLabel("<b>Cards per slide</b>"))

        self.rb_all = QRadioButton("AI decides (use all returned)")
        self.rb_range = QRadioButton("Range (random n per slide)")

        self.group_cards_per_slide = QButtonGroup(self)
        self.group_cards_per_slide.addButton(self.rb_all)
        self.group_cards_per_slide.addButton(self.rb_range)

        mode = self.cfg.get("per_slide_mode", "ai")
        self.rb_all.setChecked(mode == "ai")
        self.rb_range.setChecked(mode == "range")

        v.addWidget(self.rb_all)
        v.addWidget(self.rb_range)

        h_cards = QHBoxLayout()
        self.spin_min = QSpinBox()
        self.spin_max = QSpinBox()
        self.spin_min.setRange(1, 50)
        self.spin_max.setRange(1, 50)
        self.spin_min.setValue(int(self.cfg.get("per_slide_min", 1)))
        self.spin_max.setValue(int(self.cfg.get("per_slide_max", 3)))
        h_cards.addWidget(QLabel("Min:"))
        h_cards.addWidget(self.spin_min)
        h_cards.addWidget(QLabel("Max:"))
        h_cards.addWidget(self.spin_max)
        v.addLayout(h_cards)

        def _sync_cards_enabled():
            enabled = self.rb_range.isChecked()
            self.spin_min.setEnabled(enabled)
            self.spin_max.setEnabled(enabled)

        self.rb_all.toggled.connect(_sync_cards_enabled)
        self.rb_range.toggled.connect(_sync_cards_enabled)
        _sync_cards_enabled()

        # -----------------------
        # Page selection
        # -----------------------
        v.addSpacing(10)
        v.addWidget(QLabel("<b>Pages(applies to each selected pdf)</b>"))

        self.rb_pages_all = QRadioButton("All pages (default)")
        self.rb_pages_range = QRadioButton("Page range")

        self.group_pages = QButtonGroup(self)
        self.group_pages.addButton(self.rb_pages_all)
        self.group_pages.addButton(self.rb_pages_range)

        self.rb_pages_all.setChecked(True)

        v.addWidget(self.rb_pages_all)
        v.addWidget(self.rb_pages_range)

        h_pages = QHBoxLayout()
        self.spin_page_from = QSpinBox()
        self.spin_page_to = QSpinBox()
        self.spin_page_from.setRange(1, 100000)
        self.spin_page_to.setRange(1, 100000)
        self.spin_page_from.setValue(1)
        self.spin_page_to.setValue(99999)

        h_pages.addWidget(QLabel("From (included):"))
        h_pages.addWidget(self.spin_page_from)
        h_pages.addWidget(QLabel("To (included):"))
        h_pages.addWidget(self.spin_page_to)
        v.addLayout(h_pages)

        def _sync_pages_enabled():
            enabled = self.rb_pages_range.isChecked()
            self.spin_page_from.setEnabled(enabled)
            self.spin_page_to.setEnabled(enabled)

        self.rb_pages_all.toggled.connect(_sync_pages_enabled)
        self.rb_pages_range.toggled.connect(_sync_pages_enabled)
        _sync_pages_enabled()

        # ---- Highlighting options (image on back) ----
        self.cfg = _get_config()

        self.chk_highlight = QCheckBox("Highlight used text on back image")
        self.chk_highlight.setChecked(bool(self.cfg.get("highlight_enabled", True)))
        v.addWidget(self.chk_highlight)

        self.btn_pick_color = QPushButton("Highlight color…")
        self._color_hex = str(self.cfg.get("highlight_color_hex", "#FF69B4")).strip() or "#FF69B4"
        self._color_swatch = QLabel(f"Current: {self._color_hex}")
        self._color_swatch.setStyleSheet(f"padding:2px 6px; border:1px solid #aaa; background:{self._color_hex}; color:black;")

        from PyQt6.QtGui import QColor
        # ...
        def _pick_color():
            col = QColorDialog.getColor(QColor(self._color_hex))  # seed current value
            if col.isValid():
                self._color_hex = col.name()  # "#RRGGBB"
                self._color_swatch.setText(f"Current: {self._color_hex}")
                self._color_swatch.setStyleSheet(
                    f"padding:2px 6px; border:1px solid #aaa; background:{self._color_hex}; color:black;"
                )

        row_hl = QHBoxLayout()
        row_hl.addWidget(self.btn_pick_color)
        row_hl.addWidget(self._color_swatch)
        row_hl.addStretch(1)
        v.addLayout(row_hl)
        self.btn_pick_color.clicked.connect(_pick_color)

        # -----------------------
        # Auto-coloring
        # -----------------------
        self.chk_color_after = QCheckBox("Color deck after generation")
        self.chk_color_after.setChecked(
            bool(self.cfg.get("color_after_generation", True))
        )

        self.chk_ai_extend_colors = QCheckBox(
            "Let AI extend & normalize color table before coloring"
        )
        self.chk_ai_extend_colors.setChecked(
            bool(self.cfg.get("ai_extend_color_table", True))
        )
        v.addWidget(self.chk_ai_extend_colors)

        btn_color_table = QPushButton("Color table…")
        btn_color_table.setToolTip("Edit the word → color mapping table")

        def _open_color_table():
            from .colorizer import on_edit_color_table
            on_edit_color_table()

        btn_color_table.clicked.connect(_open_color_table)
        v.addWidget(btn_color_table)

        btn_color_settings = QPushButton("Coloration settings…")

        def _open_color_settings():
            from .colorizer import open_coloration_settings_dialog
            open_coloration_settings_dialog()

        btn_color_settings.clicked.connect(_open_color_settings)
        v.addWidget(btn_color_settings)

        v.addSpacing(10)
        v.addWidget(self.chk_color_after)

        btns = QHBoxLayout()
        ok = QPushButton("OK")
        cancel = QPushButton("Cancel")
        ok.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(ok)
        v.addLayout(btns)

    def options(self) -> dict:
        mode = "range" if self.rb_range.isChecked() else "ai"
        minv = int(self.spin_min.value())
        maxv = int(self.spin_max.value())
        if maxv < minv:
            minv, maxv = maxv, minv

        page_mode = "range" if self.rb_pages_range.isChecked() else "all"
        page_from = int(self.spin_page_from.value())
        page_to = int(self.spin_page_to.value())

        c = _get_config() 
        c["highlight_enabled"]   = self.chk_highlight.isChecked()
        c["highlight_color_hex"] = self._color_hex
        c["types_basic"] = self.chk_basic.isChecked()
        c["types_cloze"] = self.chk_cloze.isChecked()
        c["per_slide_mode"] = mode
        c["per_slide_min"] = minv
        c["per_slide_max"] = maxv
        c["color_after_generation"] = self.chk_color_after.isChecked()
        c["ai_extend_color_table"] = self.chk_ai_extend_colors.isChecked()
        c["occlusion_enabled"] = self.chk_occl.isChecked()
        _save_config(c)

        

        return {
            
            "highlight_enabled": self.chk_highlight.isChecked(),
            "highlight_color_hex": self._color_hex,
            "types_basic": self.chk_basic.isChecked(),
            "types_cloze": self.chk_cloze.isChecked(),
            "per_slide_mode": mode,
            "per_slide_min": minv,
            "per_slide_max": maxv,
            "page_mode": page_mode,
            "page_from": page_from,
            "page_to": page_to,
            "occlusion_enabled": self.chk_occl.isChecked(),
        }
# -------------------------------
# Background worker for PDF → Cards
# -------------------------------
def _worker_generate_cards(pdf_path: str, api_key: str, opts: dict) -> Dict:
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

            # Safety: swap if user reversed them
            if page_to < page_from:
                page_from, page_to = page_to, page_from

            max_pages = page_to - page_from + 1
            pages = extract_text_from_pdf(
                pdf_path,
                page_start=page_from,
                max_pages=max_pages
            )
        else:
            pages = extract_text_from_pdf(pdf_path)

        total_pages = len(pages)
        results: List[dict] = []
        page_errors: List[str] = []

        if not pages:
            return {
                "ok": True, "cards": [], "pages": 0, "errors": [],
                "meta": {"pdf_path": pdf_path}
            }

        mode = opts.get("per_slide_mode", "ai")
        minv = int(opts.get("per_slide_min", 1))
        maxv = int(opts.get("per_slide_max", 3))
        if maxv < minv:
            minv, maxv = maxv, minv

        for idx, page in enumerate(pages, start=1):
            mw.taskman.run_on_main(
                lambda i=idx, t=total_pages: ui_update(f"Processing page {i} of {t}")
            )

            # ---------- 1) Build content cards from page text ----------
            try:
                cards = []
                need_basic = opts.get("types_basic") or opts.get("types_cloze")

                if need_basic:
                    out_basic = generate_cards(page["text"], api_key, mode="basic")
                    _dbg(f"BASIC RAW CARDS (page {page['page']}): {out_basic.get('cards')}")
                    cards += out_basic.get("cards", [])

                if opts.get("types_cloze"):
                    out_cloze = generate_cards(page["text"], api_key, mode="cloze")
                    _dbg(f"CLOZE RAW OUTPUT: {out_cloze}")
                    _dbg(f"CLOZE RAW CARDS (page {page['page']}): {out_cloze.get('cards')}")
                    cards += out_cloze.get("cards", [])

            except Exception as e:
                page_errors.append(f"page {page['page']}: {e}")
                continue

            # Optional trimming for content cards (range mode)
            if mode == "range":
                if cards:
                    n = max(0, min(random.randint(minv, maxv), len(cards)))
                    cloze_first, non_cloze = [], []
                    for c in cards:
                        f = (c.get("front") or "")
                        b = (c.get("back") or "")
                        if "{{c" in (f + b):
                            cloze_first.append(c)
                        else:
                            non_cloze.append(c)
                    cards = (cloze_first + non_cloze)[:n]
                else:
                    cards = []

            # ---------- 2) Auto-occlusion near images (no UI) ----------
            try:
                if bool(opts.get("occlusion_enabled", True)):
                    # (a) find image blocks on this page (PDF points)
                    img_boxes = extract_image_boxes(pdf_path, page["page"])  # [{x,y,w,h}, ...]
                    occl_cards = []

                    # (b) render page once at known DPI (no resize)
                    page_png = _render_page_png_no_resize(pdf_path, page["page"], dpi=_OCCLUSION_DPI)

                    for r_idx, ib in enumerate(img_boxes, start=1):
                        # Expand image rect by fixed PDF-point margin to include nearby text
                        rect_pt = {
                            "x": max(0.0, ib["x"] - _IMAGE_MARGIN_PT),
                            "y": max(0.0, ib["y"] - _IMAGE_MARGIN_PT),
                            "w": ib["w"] + 2.0 * _IMAGE_MARGIN_PT,
                            "h": ib["h"] + 2.0 * _IMAGE_MARGIN_PT,
                        }

                        # (c) crop snapshot (image + adjacent text)
                        crop_png = _crop_png_region(page_png, rect_pt, dpi=_OCCLUSION_DPI)
                        if not crop_png:
                            continue

                        # (d) ask vision helper for label-like rectangles (in crop pixel coords)
                        out = suggest_occlusions_from_image(
                            crop_png, api_key,
                            max_masks=_MAX_MASKS_PER_CROP,
                            temperature=0.0
                        )
                        masks_px = (out.get("masks") if isinstance(out, dict) else []) or []

                        # (e) one card per mask ("hide one, guess one")
                        for i, m in enumerate(masks_px, start=1):
                            masked_png = _mask_one_rect_on_png(crop_png, m)
                            occl_cards.append({
                                "front": "",   # filled at insert time with <img> HTML
                                "back":  "",
                                "page": page["page"],
                                "hi":   [],    # no highlight overlays on the full slide
                                # assets carried to insert phase (main thread)
                                "_occl_assets": {
                                    "base_crop_bytes": crop_png,
                                    "masked_bytes":    masked_png,
                                    "base_name":  f"occl_p{page['page']}_r{r_idx}_base.png",
                                    "masked_name":f"occl_p{page['page']}_r{r_idx}_m{i}.png",
                                },
                                "_occl_tag": "pdf2cards:ai_occlusion"
                            })

                    # If you want to also trim occlusion cards in range mode, uncomment:
                    # if mode == "range" and occl_cards:
                    #     n = max(0, min(random.randint(minv, maxv), len(occl_cards)))
                    #     occl_cards = occl_cards[:n]

                    cards += occl_cards

            except Exception as e:
                _dbg("Auto-occlusion error: " + repr(e))

            # ---------- 3) Append to results ----------
            # Word boxes are used only for content-card highlights
            try:
                page_words = extract_words_with_boxes(pdf_path, page["page"])
            except Exception:
                page_words = []

            for card in cards:
                # A) Occlusion cards: pass through with hi=[] and private payload
                if card.get("_occl_assets"):
                    results.append({
                        "front": card.get("front", ""),
                        "back":  card.get("back",  ""),
                        "page":  page["page"],
                        "hi":    [],  # keep the full page clean for occlusion cards
                        "_occl_assets": card["_occl_assets"],
                        "_occl_tag":    card.get("_occl_tag"),
                    })
                    continue

                # B) Normal content cards: compute optional sentence highlights
                front = (card.get("front", "") or "").strip()
                back  = (card.get("back",  "") or "").strip()
                if (front and back) or ("{{c" in front) or ("{{c" in back):
                    import re
                    CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}")

                    m = CLOZE_RE.search(front) or CLOZE_RE.search(back)
                    phrase = (m.group(1).strip() if m else (back or front))

                    hl_enabled = bool(opts.get("highlight_enabled", True))
                    if hl_enabled:
                        span = 1  # one sentence default
                        hi_rects = sentence_rects_for_phrase(page_words, phrase, max_sentences=span)
                        if not hi_rects:
                            hi_rects = boxes_for_phrase(page_words, phrase)
                    else:
                        hi_rects = []

                    results.append({
                        "front": front,
                        "back":  back,
                        "page":  page["page"],
                        "hi":    hi_rects,
                    })

        return {
            "ok": True,
            "cards": results,
            "pages": total_pages,
            "errors": page_errors,
            "meta": {"pdf_path": pdf_path}
        }

    except Exception as e:
        tb = traceback.format_exc()
        return {
            "ok": False,
            "cards": [],
            "pages": 0,
            "errors": [],
            "error": str(e),
            "traceback": tb
        }
# -------------------------------
# After worker completes: insert notes, render images, apply coloring
# -------------------------------
def _on_worker_done(
    result: Dict,
    deck_id: int,
    deck_name: str,
    models: Dict[str, dict],
    opts: dict,
) -> None:

    # -------------------------------
    # Validate worker result
    # -------------------------------
    if not isinstance(result, dict) or not result.get("ok", False):
        tb = result.get("traceback", "") if isinstance(result, dict) else ""
        tb_snippet = (tb[:1200] + "\n…(truncated)…") if tb and len(tb) > 1200 else tb
        mw.progress.finish()
        showWarning(
            "Generation failed.\n\n"
            f"Error: {(result.get('error') if isinstance(result, dict) else 'unknown')}\n\n"
            f"{tb_snippet}"
        )
        return

    cards = result.get("cards", [])
    meta = result.get("meta", {}) if isinstance(result.get("meta"), dict) else {}
    pdf_path = meta.get("pdf_path")

    if not cards:
        mw.progress.finish()
        return

    col = mw.col
    col.decks.select(deck_id)

    want_basic = bool(opts.get("types_basic", True))
    want_cloze = bool(opts.get("types_cloze", False))
    if not (want_basic or want_cloze):
        mw.progress.finish()
        showWarning("No card types selected. Aborting.")
        return

    if want_basic:
        models["basic"] = ensure_basic_with_slideimage(
            models.get("basic", {}).get("name", "Basic + Slide")
        )
    if want_cloze:
        models["cloze"] = ensure_cloze_with_slideimage(
            models.get("cloze", {}).get("name", "Cloze + Slide")
        )

    mw.progress.start(label=f"Inserting {len(cards)} card(s)…", immediate=True)

    # -------------------------------
    # Main-thread work: insert notes + render images
    # -------------------------------
    def _insert_and_render():
        try:
            import os, re
            from os.path import basename

            total = len(cards)

            for idx, card in enumerate(cards, start=1):
                mw.taskman.run_on_main(
                    lambda i=idx, t=total:
                        mw.progress.update(label=f"Rendering cards… ({i}/{t})")
                )

                # Values as emitted by the worker (content cards have text here;
                # occlusion cards will be empty strings and carry _occl_assets)
                front = card.get("front", "")
                back  = card.get("back", "")
                page_no = card.get("page")
                hi_rects = card.get("hi", []) or []

                fname = ""  # full slide image filename (for SlideImage field)

                # --- If this is an occlusion card, save assets & build HTML ----
                occl_tag = None
                assets = card.get("_occl_assets")
                if assets:
                    base_name_sugg   = assets.get("base_name")   or "occl_base.png"
                    masked_name_sugg = assets.get("masked_name") or "occl_masked.png"

                    base_fn_path   = _write_media_file(base_name_sugg,   assets.get("base_crop_bytes") or b"")
                    masked_fn_path = _write_media_file(masked_name_sugg, assets.get("masked_bytes")    or b"")

                    if not (base_fn_path and masked_fn_path):
                        _dbg("Occlusion: failed to write media files; skipping this card.")
                        continue  # Skip note if we couldn't store images

                    base_fn   = os.path.basename(base_fn_path)
                    masked_fn = os.path.basename(masked_fn_path)

                    # Front = masked crop (hidden label)
                    # Back  = unmasked crop; full slide appears via SlideImage template
                    
                    front = f'<img src="{masked_fn}">'
                    back  = f'<img src="{base_fn}">'


                    occl_tag = card.get("_occl_tag")

                # ---- render slide image (always; shows on the Back via {{SlideImage}}) ----
                hl_enabled = bool(opts.get("highlight_enabled", True))
                hl_hex = str(opts.get("highlight_color_hex", "#FF69B4"))

                if pdf_path and page_no:
                    try:
                        if hl_enabled and not assets:
                            # Only content cards use sentence highlights on the full slide
                            fill_rgba = _rgba_from_hex(hl_hex, alpha=55)
                            outline_rgba = _rgba_from_hex(hl_hex, alpha=200)
                            png_bytes = render_page_as_png_with_highlights(
                                pdf_path,
                                page_no,
                                hi_rects,
                                dpi=200,
                                max_width=1600,
                                fill_rgba=fill_rgba,
                                outline_rgba=outline_rgba,
                                outline_width=2,
                            )
                        else:
                            # Occlusion cards (assets!=None) or highlight disabled: plain slide
                            res = render_page_blob(
                                pdf_path, page_no, dpi=200, max_width=1600
                            )
                            png_bytes = res[0] if res else None

                        if png_bytes:
                            base = os.path.splitext(os.path.basename(pdf_path))[0]
                            safe_deck = re.sub(r"[^A-Za-z0-9_-]+", "_", deck_name)
                            suggested = f"{safe_deck}_{base}_p{page_no}_c{idx}.png"
                            stored = _write_media_file(suggested, png_bytes)
                            fname = basename(stored) if stored else ""
                    except Exception as e:
                        _dbg(f"Image render failed: {e}")

                # ---- Decide which model to use and insert the note ----
                is_cloze = "{{c" in front or "{{c" in back

                # Occlusion cards are Basic notes by default (no {{c}})
                if is_cloze and want_cloze:
                    model = models["cloze"]
                    col.models.set_current(model)
                    note = col.newNote()
                    note.did = deck_id

                    if "{{c" in front:
                        note["Text"] = front
                        note["Back Extra"] = back
                    else:
                        note["Text"] = back
                        note["Back Extra"] = front

                    if "SlideImage" in note and fname:
                        note["SlideImage"] = f'<br><br><img src="{fname}">'
                    # Tags
                    note.tags.append("pdf2cards:ai_cloze")
                    if occl_tag:
                        note.tags.append(occl_tag)

                    col.addNote(note)
                    try:
                        force_move_cards_to_deck([c.id for c in note.cards()], deck_id)
                    except Exception:
                        pass
                    continue

                # ---- Basic note ----
                if want_basic:
                    model = models["basic"]
                    col.models.set_current(model)
                    note = col.newNote()
                    note.did = deck_id

                    note["Front"] = front
                    note["Back"]  = back

                    if "SlideImage" in note and fname:
                        note["SlideImage"] = f'<img src="{fname}">'
                    # Tags
                    note.tags.append("pdf2cards:basic")
                    if occl_tag:
                        note.tags.append(occl_tag)

                    col.addNote(note)

                    try:
                        force_move_cards_to_deck([c.id for c in note.cards()], deck_id)
                    except Exception:
                        pass

            col.save()

        except Exception as e:
            _dbg("Insert/render error: " + repr(e))

        # -------------------------------
        # After rendering → coloring phase (run on MAIN thread)
        # -------------------------------
        def _apply_color_on_main():
            try:
                mw.progress.update(label="Applying color coding…")

                try:
                    from .colorizer import apply_to_deck_ids
                except Exception as e:
                    _dbg("Colorizer module not available: " + repr(e))
                    return

                try:
                    apply_to_deck_ids([deck_id])  # must run on main thread
                except Exception as e:
                    import traceback as _tb
                    _dbg("Auto-color failed: " + repr(e) + "\n" + _tb.format_exc())

            finally:
                # Always close progress on main
                try:
                    mw.progress.finish()
                except Exception:
                    pass

        # schedule on main thread
        mw.taskman.run_on_main(_apply_color_on_main)

    # Run UI-sensitive work on main thread
    mw.taskman.run_in_background(
        _insert_and_render,
        on_done=lambda _: mw.taskman.run_on_main(mw.progress.finish)
    )
# -------------------------------
# Main entry (spawns background task)
# -------------------------------
def generate_from_pdf():
    api_key = get_api_key()
    if not api_key:
        return

    pdf_paths, _ = QFileDialog.getOpenFileNames(
        mw,
        "Select PDF(s)",
        "",
        "PDF files (*.pdf)"
    )
    if not pdf_paths:
        return

    dlg = OptionsDialog(mw)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return

    opts = dlg.options()
    if not (opts.get("types_basic") or opts.get("types_cloze")):
        showWarning("Select at least one card type (Basic or Cloze). Aborting.")
        return

    # Build models ONCE
    models: Dict[str, dict] = {}

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

    mw.progress.start(label="Generating Anki cards from PDF…", immediate=True)

    for pdf_path in pdf_paths:
        col = mw.col

        deck_name = deck_name_from_pdf_path(pdf_path)
        deck_id = get_or_create_deck(deck_name)
        col.decks.select(deck_id)

        def make_on_done(deck_id=deck_id, deck_name=deck_name):
            def _on_done(fut):
                try:
                    res = fut.result()
                except Exception as e:
                    tb = traceback.format_exc()
                    tb_snippet = (
                        tb[:1200] + "\n…(truncated)…"
                        if len(tb) > 1200 else tb
                    )
                    mw.progress.finish()
                    showWarning(
                        f"Generation failed.\n\nError: {e}\n\n{tb_snippet}"
                    )
                    return

                _on_worker_done(res, deck_id, deck_name, models, opts)
            return _on_done

        mw.taskman.run_in_background(
            lambda p=pdf_path: _worker_generate_cards(p, api_key, opts),
            on_done=make_on_done(),
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


