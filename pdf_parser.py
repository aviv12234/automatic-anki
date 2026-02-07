
# pdf_parser.py
# Extract text per page using PyMuPDF (vendored or system).
# Keeps the original API: extract_text_from_pdf(pdf_path, page_start=1, max_pages=None)
# Returns: List[{"page": int, "text": str}]

from typing import List, Dict, Optional
import os
import sys
import re

def _enable_local_pymupdf() -> None:
    """
    If a vendored PyMuPDF is present under:
        <addon>/_vendor/pymupdf/
    add it to sys.path and make DLL locations discoverable on Windows.
    """
    addon_dir = os.path.dirname(__file__)
    vendor_base = os.path.join(addon_dir, "_vendor", "pymupdf")
    if not os.path.isdir(vendor_base):
        vendor_base = os.path.join(os.path.dirname(addon_dir), "_vendor", "pymupdf")

    if os.path.isdir(vendor_base) and vendor_base not in sys.path:
        sys.path.insert(0, vendor_base)

    # Candidate DLL dirs (bundle or alongside package)
    candidates = []
    for libs_name in ("PyMuPDF.libs", "pymupdf.libs"):
        d = os.path.join(vendor_base, libs_name)
        if os.path.isdir(d):
            candidates.append(d)
    for pkg in ("pymupdf", "fitz"):
        d = os.path.join(vendor_base, pkg)
        if os.path.isdir(d):
            candidates.append(d)

    for d in candidates:
        try:
            os.add_dll_directory(d)  # type: ignore[attr-defined]
        except Exception:
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")

def _clean_text(txt: str) -> str:
    # De-hyphenate line breaks, normalize whitespace a bit
    txt = re.sub(r"(\w)-\n(\w)", r"\1\2", txt)
    txt = txt.replace("\r", "\n")
    txt = re.sub(r"[ \t]+", " ", txt)
    txt = re.sub(r"\n{2,}", "\n\n", txt)
    return txt.strip()

def _open_doc_with_pymupdf(pdf_path: str):
    """
    Import the correct module and open the document.
    Prefer 'pymupdf' (ABI3 wheel), then fallback to 'fitz' compatibility layer.
    """
    _enable_local_pymupdf()

    pm = None
    # Try the modern / compiled module first
    try:
        import pymupdf as pm  # type: ignore
    except Exception:
        pm = None

    if pm is not None:
        if hasattr(pm, "open"):
            return pm.open(pdf_path)
        if hasattr(pm, "Document"):
            return pm.Document(pdf_path)

    # Fallback: a 'fitz' helper may exist; use whatever constructor it exposes
    try:
        import fitz as f  # type: ignore
        if hasattr(f, "open"):
            return f.open(pdf_path)
        if hasattr(f, "Document"):
            return f.Document(pdf_path)
    except Exception:
        pass

    raise ModuleNotFoundError(
        "Could not open PDF with PyMuPDF. Ensure '_vendor/pymupdf/pymupdf' contains "
        "_mupdf.pyd and the required DLL(s) like 'mupdfcpp64.dll'."
    )

def extract_text_from_pdf(
    pdf_path: str,
    page_start: int = 1,
    max_pages: Optional[int] = None
) -> List[Dict[str, str]]:
    """
    Extract per-page text with PyMuPDF. No dependency on 'pypdf'.
    """
    doc = _open_doc_with_pymupdf(pdf_path)

    # Page count across pymupdf/fitz variants
    total = getattr(doc, "page_count", None)
    if total is None:
        # older API: len(doc)
        total = len(doc)

    start_idx = max(1, page_start)
    end_idx = total if max_pages is None else min(total, start_idx + max_pages - 1)

    out: List[Dict[str, str]] = []
    for pg in range(start_idx, end_idx + 1):
        page = doc[pg - 1]
        # Prefer "text" extractor; fallback to default
        try:
            txt = page.get_text("text")
        except Exception:
            txt = page.get_text()
        txt = _clean_text(txt or "")
        if txt:
            out.append({"page": pg, "text": txt})
    return out

# --- New: word boxes + phrase matching (PDF points) -------------------------
from typing import List, Dict, Optional, Tuple
import re

def extract_words_with_boxes(pdf_path: str, page_number: int) -> List[Dict]:
    """
    Return a list of word dicts for the given page:
    [
      {
        "text": "...", "x0": float, "y0": float, "x1": float, "y1": float,
        "block": int, "line": int, "word_no": int, "ends_sent": bool
      }, ...
    ]
    Coordinates are in PDF points (1/72 inch).
    """
    doc = _open_doc_with_pymupdf(pdf_path)
    page = doc[page_number - 1]
    try:
        # (x0, y0, x1, y1, "text", block, line, word_no)
        words = page.get_text("words")
    except Exception:
        words = []

    out: List[Dict] = []
    SENT_END_RE = re.compile(r'[\.!\?…][)"\]]*$')

    for w in words:
        if len(w) >= 5:
            x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4] or ""
            if not str(txt).strip():
                continue
            block = int(w[5]) if len(w) > 5 else 0
            line  = int(w[6]) if len(w) > 6 else 0
            wno   = int(w[7]) if len(w) > 7 else 0
            ends  = bool(SENT_END_RE.search(str(txt).strip()))
            out.append({
                "text": str(txt),
                "x0": float(x0), "y0": float(y0), "x1": float(x1), "y1": float(y1),
                "block": block, "line": line, "word_no": wno, "ends_sent": ends
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