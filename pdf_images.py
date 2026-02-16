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
    """
    Render a PDF page to PNG and paint highlight rectangles on top.
    The input rects may be:
      • PDF points (72 dpi)  -> used as-is
      • pixel units (at 'dpi')-> scaled by 72/dpi
      • relative fractions [0..1] -> scaled by page width/height
    Also clamps and filters suspicious rects to avoid page-wide floods.
    """
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_number - 1]
        page_rect = page.rect

        # Normalize RGBA into 0..1
        def _norm(rgba):
            r, g, b, a = rgba
            return (r / 255.0, g / 255.0, b / 255.0, a / 255.0)

        fr, fg, fb, fa = _norm(fill_rgba)
        or_, og, ob, oa = _norm(outline_rgba)

        # Heuristic: convert various incoming rect formats into PDF points
        def _as_points(r, rect_count):
            if isinstance(r, dict):
                x = float(r.get("x", 0.0))
                y = float(r.get("y", 0.0))
                w = float(r.get("w", 0.0))
                h = float(r.get("h", 0.0))
                x1, y1 = x + w, y + h
            else:
                x, y, x1, y1 = map(float, r)
                w, h = x1 - x, y1 - y

            # Degenerate -> drop
            if w <= 0 or h <= 0:
                return None

            # 1) Relative fractions 0..1 ?
            is_rel = all(0.0 <= v <= 1.2 for v in (x, y, w, h))
            # 2) Way bigger than page -> likely pixels at 'dpi'
            is_px = (
                x > page_rect.width * 1.5
                or y > page_rect.height * 1.5
                or w > page_rect.width * 1.5
                or h > page_rect.height * 1.5
            )

            if is_rel:
                x *= page_rect.width
                y *= page_rect.height
                w *= page_rect.width
                h *= page_rect.height
                x1, y1 = x + w, y + h
            elif is_px:
                scale = 72.0 / float(dpi or 72.0)
                x *= scale; y *= scale; x1 *= scale; y1 *= scale

            # Clamp to page bounds
            x0 = max(page_rect.x0, min(x, x1))
            y0 = max(page_rect.y0, min(y, y1))
            x1 = min(page_rect.x1, max(x, x1))
            y1 = min(page_rect.y1, max(y, y1))
            if x1 - x0 < 1.0 or y1 - y0 < 1.0:
                return None

            # If a rect is ~full-page and there are other rects,
            # treat it as suspicious and drop it.
            page_area = page_rect.width * page_rect.height
            area = (x1 - x0) * (y1 - y0)
            if rect_count > 1 and area > 0.97 * page_area:
                return None

            return fitz.Rect(x0, y0, x1, y1)

        rects = rects or []
        norm_rects = []
        for r in rects:
            nr = _as_points(r, len(rects))
            if nr is not None:
                norm_rects.append(nr)

        # Log what we ended up with
        try:
            _dbg(f"Highlights: in={len(rects)}, normalized={len(norm_rects)} @page={page_number}")
        except Exception:
            pass

        # Create in-memory annotations
        for rr in norm_rects:
            annot = page.add_rect_annot(rr)
            annot.set_colors(stroke=(or_, og, ob), fill=(fr, fg, fb))
            annot.set_opacity(fa)  # overall opacity
            annot.set_border(width=outline_width)
            annot.update()

        # Render
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png = pix.tobytes("png")

        # Optional resize with Qt (if available)
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
        except Exception:
            pass

        return png
    finally:
        doc.close()