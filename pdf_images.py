# pdf_images.py — robust import for PyMuPDF on Anki
# - No venv, no pip
# - Tries: pymupdf -> fitz -> optional _vendor fallback (wheel you provide)
# - Qt PDF rendering disabled (use PyMuPDF only)
# - Fast highlight drawing (render once, draw on same pixmap)

from typing import Optional

# --- debug logger ---
def _dbg(msg: str) -> None:
    try:
        from aqt import mw
        import os, time
        path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [pdf_images] {msg}\n")
    except Exception:
        pass


# ------------------------------------------------------------------------
# Minimal PNG resize (Qt only — safe)
# ------------------------------------------------------------------------
def _resize_png_qt(png_bytes: bytes, max_width: int = 1600) -> bytes:
    try:
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
    except Exception:
        return png_bytes

    img = QImage.fromData(png_bytes)
    if img.isNull() or img.width() <= max_width:
        return png_bytes

    new_h = max(1, int((img.height() * max_width) / img.width()))
    scaled = img.scaled(max_width, new_h)

    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    scaled.save(buf, b"PNG")
    buf.close()
    return bytes(ba)


# ------------------------------------------------------------------------
# Embedded image fallback (pure Python via pypdf)
# ------------------------------------------------------------------------
def _extract_largest_embedded_image(pdf_path: str, page_number: int) -> Optional[bytes]:
    try:
        from pypdf import PdfReader
    except Exception:
        return None

    try:
        reader = PdfReader(pdf_path)
        page = reader.pages[page_number - 1]
        images = getattr(page, "images", None)
        if not images:
            return None

        largest = None
        max_px = 0
        for img in images:
            data = getattr(img, "data", None)
            w = getattr(img, "width", 0)
            h = getattr(img, "height", 0)
            if isinstance(data, (bytes, bytearray)) and w and h:
                px = w * h
                if px > max_px:
                    largest = bytes(data)
                    max_px = px
        return largest
    except Exception:
        return None


# ------------------------------------------------------------------------
# Robust PyMuPDF import helper:
#   1) import pymupdf as fitz
#   2) import fitz
#   3) load from _vendor/pymupdf (if a wheel was previously extracted)
#      or if you placed a ready-made 'fitz' or 'pymupdf' package there.
# ------------------------------------------------------------------------
def _import_fitz():
    try:
        import pymupdf as fitz  # new name
        _dbg("Imported PyMuPDF via 'pymupdf'")
        return fitz
    except Exception:
        pass

    try:
        import fitz  # legacy name
        _dbg("Imported PyMuPDF via legacy 'fitz'")
        return fitz
    except Exception:
        pass

    # Try optional vendor path (_vendor/pymupdf or _vendor/fitz pre-extracted)
    try:
        import sys, os
        base = os.path.dirname(__file__)
        vendor_pkg_dir = os.path.join(base, "_vendor")
        # Prefer a nested package dir if present (e.g., _vendor/pymupdf or _vendor/fitz)
        # Add both to sys.path if they exist.
        cand = []
        for name in ("pymupdf", "fitz"):
            p = os.path.join(vendor_pkg_dir, name)
            if os.path.isdir(p):
                cand.append(p)
        if os.path.isdir(vendor_pkg_dir) and vendor_pkg_dir not in sys.path:
            sys.path.insert(0, vendor_pkg_dir)
        for p in cand:
            if p not in sys.path:
                sys.path.insert(0, p)

        try:
            import pymupdf as fitz
            _dbg("Imported PyMuPDF from _vendor via 'pymupdf'")
            return fitz
        except Exception:
            pass

        import fitz
        _dbg("Imported PyMuPDF from _vendor via 'fitz'")
        return fitz
    except Exception as e:
        _dbg(f"PyMuPDF import failed (no pymupdf/fitz, no vendor): {repr(e)}")
        raise ImportError(
            "PyMuPDF not found. This Anki build exposes neither 'pymupdf' nor 'fitz'. "
            "Either update Anki, or place a compatible PyMuPDF wheel/package under "
            "automatic-anki/_vendor/ and restart Anki."
        )


# Grab the module (as 'fitz')
fitz = _import_fitz()


# ------------------------------------------------------------------------
# PyMuPDF rendering
# ------------------------------------------------------------------------
def _render_with_pymupdf(pdf_path: str, page_number: int, dpi: int) -> Optional[bytes]:
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        out = pix.tobytes("png")
        _dbg(f"PyMuPDF render OK ({pix.width}x{pix.height}@{dpi}dpi)")
        return out
    except Exception as e:
        _dbg(f"PyMuPDF render failed: {repr(e)}")
        return None


# ------------------------------------------------------------------------
# PUBLIC API: render_page_as_png
# (Qt disabled; PyMuPDF + image fallback)
# ------------------------------------------------------------------------
def render_page_as_png(
    pdf_path: str,
    page_number: int,
    dpi: int = 200,
    max_width: int = 2000,
) -> Optional[bytes]:

    _dbg("Qt disabled — using PyMuPDF only")

    # PyMuPDF (built-in or vendor)
    png = _render_with_pymupdf(pdf_path, page_number, dpi)
    if png:
        return _resize_png_qt(png, max_width=max_width)
    # Fallback to embedded images
    blob = _extract_largest_embedded_image(pdf_path, page_number)
    if blob:
        _dbg("Using embedded image fallback")
        return blob

    _dbg("render_page_as_png: all paths failed")
    return None
def render_page_as_png_with_highlights(
    pdf_path,
    page_number,
    rects,
    dpi=200,
    max_width=2000,
    fill_rgba=(255, 255, 0, 80),
    outline_rgba=(255, 0, 0, 200),
    outline_width=2,
):
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    page = doc[page_number - 1]

    # Normalize RGBA into 0..1
    def _norm(rgba):
        r, g, b, a = rgba
        return (r/255.0, g/255.0, b/255.0, a/255.0)

    fr, fg, fb, fa = _norm(fill_rgba)
    or_, og, ob, oa = _norm(outline_rgba)

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    # ----- CREATE IN-MEMORY ANNOTATIONS -----
    for r in rects:
        if isinstance(r, dict):
            x0 = r["x"]
            y0 = r["y"]
            x1 = x0 + r["w"]
            y1 = y0 + r["h"]
        else:
            x0, y0, x1, y1 = r

        rect_obj = fitz.Rect(x0, y0, x1, y1)

        annot = page.add_rect_annot(rect_obj)
        annot.set_colors(stroke=(or_, og, ob), fill=(fr, fg, fb))
        annot.set_opacity(fa)  # fill opacity
        annot.set_border(width=outline_width)
        annot.update()

    # ----- RENDER PAGE WITH ANNOTATIONS -----
    pix = page.get_pixmap(matrix=mat, alpha=False)
    png = pix.tobytes("png")

    # ----- RESIZE (Qt) -----
    try:
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice

        img = QImage.fromData(png)
        if img.width() > max_width:
            nh = int(img.height() * (max_width / img.width()))
            scaled = img.scaled(max_width, nh)

            ba = QByteArray()
            buf = QBuffer(ba)
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            scaled.save(buf, b"PNG")
            buf.close()

            return bytes(ba)
    except:
        pass

    return png