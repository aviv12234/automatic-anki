
# pdf_parser.py
# Extract text per page using PyMuPDF (vendored or system).
# Keeps the original API: extract_text_from_pdf(pdf_path, page_start=1, max_pages=None)
# Returns: List[{"page": int, "text": str}]
from typing import List, Dict, Optional
from .pdf_images import render_page_as_png
from .openai_cards import ocr_page_image

from typing import List, Dict, Optional
import os
import sys
import re
# pdf_parser.py
from typing import List, Dict, Optional, Tuple   # <-- add Tuple here

def _clean_text(txt: str) -> str:
    # De-hyphenate line breaks, normalize whitespace a bit
    txt = re.sub(r"(\w)-\n(\w)", r"\1\2", txt)
    txt = txt.replace("\r", "\n")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{2,}", "\n\n", txt)
    return txt.strip()



def extract_text_from_pdf(
    pdf_path: str,
    api_key: str,
    page_start: int = 1,
    max_pages: Optional[int] = None,
) -> List[Dict[str, str]]:
    """
    OCR-first extractor: for each page, render PNG and send to OpenAI Vision.
    Returns: [{"page": int, "text": str}, ...]
    This avoids all native dependencies and works on any PDF (slides, scans, etc.).
    """
    # Determine how many pages we have
    total_pages = 0

    # Try QtPdf to get pageCount
    try:
        # pdf_parser.py  --- replace the QtPdf pageCount block with this
        try:
            from PyQt6.QtPdf import QPdfDocument
            qdoc = QPdfDocument()
            qdoc.load(pdf_path)  # load() returns None; check status() after
            if qdoc.status() == QPdfDocument.Status.Ready:
                total_pages = int(qdoc.pageCount())
        except Exception:
            pass
    except Exception:
        pass

    # Fallback: pypdf just for counting pages (optional, pure python)
    if total_pages <= 0:
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            total_pages = len(reader.pages)
        except Exception:
            total_pages = 0

    results: List[Dict[str, str]] = []

    if total_pages <= 0:
        cap = 500
        idx = max(1, int(page_start))
        remaining = int(max_pages or cap)

        while remaining > 0 and idx <= cap:
            png = render_page_as_png(pdf_path, idx, dpi=300, max_width=4000)

            if not png:
                # No more valid pages → stop the loop
                break

            # Valid PNG → OCR it
            text = ocr_page_image(png, api_key) or ""
            results.append({"page": idx, "text": text})

            idx += 1
            remaining -= 1

        return results

    # We know the total page count
    start = max(1, int(page_start))
    end = total_pages if max_pages is None else min(total_pages, start + int(max_pages) - 1)

    for p in range(start, end + 1):
        png = render_page_as_png(pdf_path, p, dpi=300, max_width=4000)

        # NEW debug line:
        try:
            from aqt import mw
            import os, time
            path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] RENDER page {p}: png={('None' if not png else len(png))} bytes\n")
        except:
            pass

        if not png:
            results.append({"page": p, "text": ""})
            continue
        
        from .openai_cards import _limit_png_size_for_vision
        png = _limit_png_size_for_vision(png, max_bytes=3_500_000)
        text = ocr_page_image(png, api_key) or ""
        try:
            # Use main.py’s logger path/format for consistency
            from aqt import mw
            import time, os
            path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] OCR page {p}: {len(text)} chars\n")
        except Exception:
            pass

        results.append({"page": p, "text": text})

    return results

# pdf_parser.py
from typing import List, Dict
import re

def extract_words_with_boxes(pdf_path: str, page_number: int) -> List[Dict]:
    """
    Return a list of word dicts for the given page, in PDF points:
    [
      { "text": "...", "x0": float, "y0": float, "x1": float, "y1": float,
        "block": int, "line": int, "word_no": int, "ends_sent": bool },
      ...
    ]
    """
    try:
        try:
            import pymupdf as fitz
        except Exception:
            import fitz  # legacy
    except Exception:
        return []

    try:
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]              # 1-based -> 0-based
        words = page.get_text("words")           # (x0,y0,x1,y1, "w", block, line, word_no)
    except Exception:
        return []

    out: List[Dict] = []
    for w in words:
        if len(w) < 8:
            continue
        x0, y0, x1, y1, text, block, line, word_no = w[:8]
        text_s = str(text or "")
        ends = bool(re.search(r"[.!?;:]\s*$", text_s))
        out.append({
            "text": text_s, "x0": float(x0), "y0": float(y0),
            "x1": float(x1), "y1": float(y1),
            "block": int(block), "line": int(line), "word_no": int(word_no),
            "ends_sent": ends,
        })
    return out


def _merge_rects(rects: List[Tuple[float, float, float, float]],
                 pad: float = 0.5) -> Tuple[float, float, float, float]:
    """Return a single bounding rectangle (x0,y0,x1,y1) that encloses rects."""
    if not rects:
        return (0.0, 0.0, 0.0, 0.0)
    x0 = min(r[0] for r in rects) - pad
    y0 = min(r[1] for r in rects) - pad
    x1 = max(r[2] for r in rects) + pad
    y1 = max(r[3] for r in rects) + pad
    return (x0, y0, x1, y1)

def _normalize_token(t: str) -> str:
    return re.sub(r"[^\wα-ωΑ-Ωµ²³⁺⁻]+", "", t.lower())

def sentence_rects_for_phrase(
    words: List[Dict],
    phrase: str,
    max_sentences: int = 1,
    pad: float = 0.5,
) -> List[Dict]:
    """
    Find the phrase, then expand to sentence boundary(ies) and return line-tight rectangles.
    Returns: [{"x":..., "y":..., "w":..., "h":...}, ...] in PDF points.
    - max_sentences: include at least 1 (default) and at most this many sentences (1..3 recommended).
    - Multiple rectangles are returned if the sentence wraps across lines.
    """
    phrase = (phrase or "").strip()
    if not phrase or not words:
        return []

    # Build normalized sequences for matching
    raw_tokens = [w["text"] for w in words]
    norm_words = [_normalize_token(t) for t in raw_tokens]
    tokens = [t for t in re.split(r"\s+", phrase) if _normalize_token(t)]
    norm_tokens = [_normalize_token(t) for t in tokens]
    if not norm_tokens:
        return []

    # Exact contiguous match first
    hits: List[Tuple[int, int]] = []
    L = len(norm_tokens)
    for i in range(0, max(0, len(norm_words) - L + 1)):
        if norm_words[i:i + L] == norm_tokens:
            hits.append((i, i + L - 1))

    # Choose the span we will expand
    if hits:
        i0, i1 = hits[0]
    else:
        # Fallback: cover min..max indices of distinctive tokens found
        key = sorted(set(norm_tokens), key=lambda t: (-len(t), t))
        idxs = [j for j, wnorm in enumerate(norm_words) if wnorm in key]
        if not idxs:
            return []
        i0, i1 = min(idxs), max(idxs)

    # Keep expansion inside the same block for stability
    base_block = words[i0].get("block", 0)

    # Expand LEFT to previous sentence ending within the same block
    s = i0
    j = i0 - 1
    while j >= 0 and words[j].get("block", 0) == base_block:
        if words[j].get("ends_sent", False):
            s = j + 1
            break
        s = j
        j -= 1

    # Expand RIGHT across up to max_sentences endings (at least 1 sentence)
    e = i1
    sentences_taken = 0
    k = i1
    while k < len(words) and words[k].get("block", 0) == base_block:
        e = k
        if words[k].get("ends_sent", False):
            sentences_taken += 1
            if sentences_taken >= max(1, int(max_sentences or 1)):
                break
        k += 1

    # Merge words into rectangles per (block,line) for tight, line-wrapped boxes
    rects_by_line: Dict[Tuple[int, int], List[Tuple[float, float, float, float]]] = {}
    for idx in range(s, e + 1):
        w = words[idx]
        key = (w.get("block", 0), w.get("line", 0))
        rects_by_line.setdefault(key, []).append((w["x0"], w["y0"], w["x1"], w["y1"]))

    out_rects: List[Dict] = []
    for _, rects in sorted(rects_by_line.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        x0 = min(r[0] for r in rects) - pad
        y0 = min(r[1] for r in rects) - pad
        x1 = max(r[2] for r in rects) + pad
        y1 = max(r[3] for r in rects) + pad
        out_rects.append({"x": x0, "y": y0, "w": (x1 - x0), "h": (y1 - y0)})

    return out_rects

def boxes_for_phrase(words: List[Dict], phrase: str) -> List[Dict]:
    """
    Locate 'phrase' on the page and return a list of rectangles (one per occurrence)
    in PDF points: [{"x":..., "y":..., "w":..., "h":...}, ...].

    Strategy:
      - Case-insensitive, punctuation-light tokenization on both phrase and page words.
      - Exact sequence match -> one tight rectangle per hit.
      - If no exact sequence is found, fall back to highlighting a few distinctive tokens.
    """
    phrase = (phrase or "").strip()
    if not phrase:
        return []

    tokens = [t for t in re.split(r"\s+", phrase) if _normalize_token(t)]
    norm_tokens = [_normalize_token(t) for t in tokens]
    if not norm_tokens:
        return []

    norm_words = [_normalize_token(w["text"]) for w in words]

    hits: List[Tuple[int, int]] = []
    L = len(norm_tokens)
    for i in range(0, max(0, len(norm_words) - L + 1)):
        if norm_words[i:i+L] == norm_tokens:
            hits.append((i, i+L-1))

    rects: List[Dict] = []
    if hits:
        # Return ONE rectangle PER occurrence (tight around the whole phrase)
        for (i0, i1) in hits:
            xs = [words[k]["x0"] for k in range(i0, i1+1)]
            ys = [words[k]["y0"] for k in range(i0, i1+1)]
            xe = [words[k]["x1"] for k in range(i0, i1+1)]
            ye = [words[k]["y1"] for k in range(i0, i1+1)]
            x0, y0, x1, y1 = min(xs), min(ys), max(xe), max(ye)
            rects.append({"x": x0, "y": y0, "w": (x1 - x0), "h": (y1 - y0)})
        return rects

    # ✅ Fallback: merge all matching tokens into ONE phrase rectangle
    key_tokens = sorted(set(norm_tokens), key=lambda t: (-len(t), t))

    matched_boxes = []
    for j, wnorm in enumerate(norm_words):
        if wnorm in key_tokens:
            w = words[j]
            matched_boxes.append(w)

    if not matched_boxes:
        return []

    x0 = min(w["x0"] for w in matched_boxes)
    y0 = min(w["y0"] for w in matched_boxes)
    x1 = max(w["x1"] for w in matched_boxes)
    y1 = max(w["y1"] for w in matched_boxes)

    return [{
        "x": x0,
        "y": y0,
        "w": (x1 - x0),
        "h": (y1 - y0),
    }]

# --- NEW: image boxes per page (PDF points) -------------------------------
from typing import List, Dict

def extract_image_boxes(pdf_path: str, page_number: int) -> List[Dict]:
    """
    Return [{ "x":..., "y":..., "w":..., "h":... }, ...] for each image block
    on the page, using PyMuPDF rawdict ("type": 1 or "image"). Coordinates are
    in PDF points (1/72 inch).
    """

    return []
