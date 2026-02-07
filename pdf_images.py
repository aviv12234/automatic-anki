
# pdf_images.py
from typing import Optional
from io import BytesIO


from typing import List, Dict, Tuple, Optional

# Pillow is available in Anki's Python environment
from PIL import Image

def _resize_to_max_width(png_bytes: bytes, max_width: int = 1600) -> bytes:
    """
    Downscale wide images to keep media sizes reasonable; return PNG bytes.
    """
    try:
        with Image.open(BytesIO(png_bytes)) as im:
            if im.width <= max_width:
                return png_bytes
            new_h = int(im.height * max_width / im.width)
            im = im.convert("RGB").resize((max_width, new_h), Image.LANCZOS)
            out = BytesIO()
            im.save(out, format="PNG", optimize=True)
            return out.getvalue()
    except Exception:
        return png_bytes

def _render_with_pymupdf(pdf_path: str, page_number: int, dpi: int = 200) -> Optional[bytes]:
    """
    Render the full page to a PNG using PyMuPDF if present.
    """
    try:
        import fitz  # PyMuPDF
    except Exception:
        return None

    try:
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]  # 1-based -> 0-based
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")
        return _resize_to_max_width(png_bytes)
    except Exception:
        return None

def _extract_largest_embedded_image(pdf_path: str, page_number: int) -> Optional[bytes]:
    """
    Fallback: for PDFs exported as images-per-slide, take the largest embedded image.
    Convert to PNG to ensure consistent <img> handling.
    """
    try:
        from pypdf import PdfReader
    except Exception:
        return None

    try:
        reader = PdfReader(pdf_path)
        page = reader.pages[page_number - 1]

        # pypdf >= 3.x exposes images via page.images (best case).
        images = getattr(page, "images", None)
        candidates = []
        if images:
            for img in images:
                # img.data: bytes, img.width, img.height may be available
                data = getattr(img, "data", None)
                w = getattr(img, "width", 0) or 0
                h = getattr(img, "height", 0) or 0
                if isinstance(data, (bytes, bytearray)) and w and h:
                    candidates.append((w * h, bytes(data)))
        # If the attribute is unavailable or empty, we stop here.
        if not candidates:
            return None

        _, best = max(candidates, key=lambda x: x[0])
        try:
            with Image.open(BytesIO(best)) as im:
                im = im.convert("RGB")
                out = BytesIO()
                im.save(out, format="PNG", optimize=True)
                return _resize_to_max_width(out.getvalue())
        except Exception:
            # If Pillow can't decode, just return the raw bytes (not ideal).
            return best
    except Exception:
        return None

def render_page_as_png(pdf_path: str, page_number: int, dpi: int = 200, max_width: int = 1600) -> Optional[bytes]:
    """
    Try full-page render with PyMuPDF first; otherwise, grab the largest embedded image.
    Returns PNG bytes or None if unavailable.
    """
    png = _render_with_pymupdf(pdf_path, page_number, dpi=dpi)
    if png:
        return _resize_to_max_width(png, max_width=max_width)
    png = _extract_largest_embedded_image(pdf_path, page_number)
    if png:
        return _resize_to_max_width(png, max_width=max_width)
    return None


# add to imports
from typing import List, Dict, Tuple, Optional

def render_page_as_png_with_highlights(
    pdf_path: str,
    page_number: int,
    rects_points: List[Dict],   # [{"x":..,"y":..,"w":..,"h":..}] in PDF points
    dpi: int = 200,
    max_width: int = 1600,
    fill_rgba: Optional[Tuple[int, int, int, int]] = None,
    outline_rgba: Optional[Tuple[int, int, int, int]] = None,
    outline_width: int = 2,
) -> Optional[bytes]:
    """Render the given page and paint translucent rectangles over it. Returns PNG bytes (or None if rendering fails)."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        # No fitz; fall back to plain render (no highlights)
        return render_page_as_png(pdf_path, page_number, dpi=dpi, max_width=max_width)

    # default colors (keep your current pink as default)
    if fill_rgba is None:
        fill_rgba = (255, 105, 180, 55)     # light translucent
    if outline_rgba is None:
        outline_rgba = (255, 80, 150, 180)  # stronger outline

    try:
        # 1) Render page to PNG
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png_bytes = pix.tobytes("png")

        # 2) Draw overlays with Pillow
        from PIL import Image, ImageDraw
        im = Image.open(BytesIO(png_bytes)).convert("RGBA")
        draw = ImageDraw.Draw(im, "RGBA")

        for r in rects_points or []:
            x = int((r["x"]) * zoom)
            y = int((r["y"]) * zoom)
            w = int(max(1, r["w"] * zoom))
            h = int(max(1, r["h"] * zoom))

            # draw translucent fill on separate layer to preserve alpha
            overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay, "RGBA")
            odraw.rectangle([x, y, x + w, y + h], fill=fill_rgba)

            # crisp outline directly on base image
            draw.rectangle([x, y, x + w, y + h], outline=outline_rgba, width=outline_width)

            # composite overlay onto image
            im = Image.alpha_composite(im, overlay)
            draw = ImageDraw.Draw(im, "RGBA")

        out = BytesIO()
        im.save(out, format="PNG", optimize=True)
        data = out.getvalue()
        return _resize_to_max_width(data, max_width=max_width)
    except Exception:
        # If anything fails, return plain page image
        return render_page_as_png(pdf_path, page_number, dpi=dpi, max_width=max_width)