# pdf_parser.py — SEMANTIC HIGHLIGHTING + OCR (fixed)

from typing import List, Dict, Optional, Tuple
import re
import math
import requests

from .pdf_images import render_page_as_png
from .openai_cards import ocr_page_image, _limit_png_size_for_vision

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------

HIGHLIGHT_MAX_SENTENCES = 2
HIGHLIGHT_PAGE_AREA_LIMIT = 0.70

TITLE_FONT_SCALE = 1.35
TITLE_TOP_FRACTION = 0.20
CAPTION_PREFIXES = r"^(fig(ure)?\.?|table|diagram|schematic)\b"

# -------------------------------------------------------------------
# Utility: cosine similarity
# -------------------------------------------------------------------

def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (
        math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) + 1e-9
    )

def embed_texts(texts, api_key):
    """
    Uses the correct OpenAI endpoint for gpt-4o-mini-embed:
    POST /v1/responses with type=input_text.
    """
    import requests
    url = "https://api.openai.com/v1/responses"

    payload = {
        "model": "gpt-4o-mini-embed",
        "input": [
            {
                "type": "input_text",
                "text": txt
            }
            for txt in texts
        ],
        "encoding_format": "float"
    }

    resp = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=45
    )

    resp.raise_for_status()
    data = resp.json()

    # Extract embeddings
    embeddings = []
    for item in data.get("output", []):
        emb = item.get("embedding")
        if emb:
            embeddings.append(emb)

    return embeddings

# -------------------------------------------------------------------
# OCR TEXT EXTRACTION (restored)
# -------------------------------------------------------------------
def extract_text_from_pdf(
    pdf_path: str,
    api_key: str,
    page_start: int = 1,
    max_pages: Optional[int] = None,
) -> List[Dict[str, str]]:
    """
    Return [{"page": int, "text": str}, ...] using page PNG + OCR.
    Uses QtPdf/pypdf for page count when possible; otherwise scans until failure.
    """
    total_pages = 0

    # QtPdf (preferred)
    try:
        from PyQt6.QtPdf import QPdfDocument
        qdoc = QPdfDocument()
        qdoc.load(pdf_path)
        if qdoc.status() == QPdfDocument.Status.Ready:
            total_pages = int(qdoc.pageCount())
    except Exception:
        pass

    # pypdf fallback
    if total_pages <= 0:
        try:
            from pypdf import PdfReader
            reader = PdfReader(pdf_path)
            total_pages = len(reader.pages)
        except Exception:
            total_pages = 0

    results: List[Dict[str, str]] = []

    # Unknown count: iterate until render fails
    if total_pages <= 0:
        cap = 500
        idx = max(1, int(page_start))
        remaining = int(max_pages or cap)
        while remaining > 0 and idx <= cap:
            png = render_page_as_png(pdf_path, idx, dpi=300, max_width=4000)
            if not png:
                break
            png = _limit_png_size_for_vision(png, max_bytes=3_500_000)
            text = ocr_page_image(png, api_key) or ""
            results.append({"page": idx, "text": text})
            idx += 1
            remaining -= 1
        return results

    # Known count path
    start = max(1, int(page_start))
    end = total_pages if max_pages is None else min(total_pages, start + int(max_pages) - 1)
    for p in range(start, end + 1):
        png = render_page_as_png(pdf_path, p, dpi=300, max_width=4000)
        if not png:
            results.append({"page": p, "text": ""})
            continue
        png = _limit_png_size_for_vision(png, max_bytes=3_500_000)
        text = ocr_page_image(png, api_key) or ""
        results.append({"page": p, "text": text})

    return results

# -------------------------------------------------------------------
# Line layout indexing
# -------------------------------------------------------------------

def _index_line_layout(page):
    """
    Extract (block,line) → avg font size, y-position, text.
    """
    info = {}
    try:
        data = page.get_text("dict")
        b_idx = -1
        for b in data.get("blocks", []):
            b_idx += 1
            if b.get("type", 0) != 0:
                continue
            lines = b.get("lines", [])
            for l_idx, ln in enumerate(lines):
                spans = ln.get("spans", [])
                if not spans:
                    continue
                sizes = [float(s.get("size", 0.0)) for s in spans if s.get("text")]
                line_text = "".join(s.get("text", "") for s in spans).strip()
                x0, y0, x1, y1 = ln.get("bbox", [0, 0, 0, 0])
                size_avg = sum(sizes) / max(1, len(sizes))
                info[(b_idx, l_idx)] = {
                    "size_avg": size_avg,
                    "y0": float(y0),
                    "text": line_text,
                }
    except Exception:
        pass
    return info

# -------------------------------------------------------------------
# Word extraction WITH layout metadata (title/caption detection)
# -------------------------------------------------------------------

def extract_words_with_boxes(pdf_path: str, page_number: int) -> List[Dict]:
    try:
        try:
            import pymupdf as fitz
        except Exception:
            import fitz
    except Exception:
        return []

    try:
        doc = fitz.open(pdf_path)
        page = doc[page_number - 1]
    except Exception:
        return []

    line_info = _index_line_layout(page)

    # words: (x0,y0,x1,y1, "text", block, line, word_no)
    try:
        words_raw = page.get_text("words")
    except Exception:
        return []

    # median font size
    sizes = [v["size_avg"] for v in line_info.values() if v.get("size_avg")]
    median_size = sorted(sizes)[len(sizes)//2] if sizes else 0.0

    page_h = float(page.rect.height or 1.0)
    caption_re = re.compile(CAPTION_PREFIXES, flags=re.I)

    out = []
    for w in words_raw:
        if len(w) < 8:
            continue
        x0, y0, x1, y1, text, block, line, word_no = w[:8]
        li = line_info.get((int(block), int(line)), {})

        line_size = float(li.get("size_avg", median_size))
        line_y0 = float(li.get("y0", y0))
        line_text = li.get("text", "")

        # Title?
        is_title = False
        if median_size > 0:
            if line_y0 <= page_h * TITLE_TOP_FRACTION and line_size >= median_size * TITLE_FONT_SCALE:
                if len(line_text.split()) <= 12:
                    is_title = True

        # Caption?
        is_caption = bool(caption_re.match(line_text.strip()))

        ends = bool(re.search(r"[.!?;:]\s*$", str(text or "")))

        out.append({
            "text": str(text or ""),
            "x0": float(x0), "y0": float(y0),
            "x1": float(x1), "y1": float(y1),
            "block": int(block), "line": int(line),
            "word_no": int(word_no),
            "ends_sent": ends,

            # layout metadata
            "line_size": line_size,
            "line_y0": line_y0,
            "line_text": line_text,
            "is_title_line": is_title,
            "is_caption_line": is_caption,
        })

    return out

# -------------------------------------------------------------------
# SEMANTIC SENTENCE → RECTANGLES
# -------------------------------------------------------------------
def semantic_sentence_rects(
    words,
    answer_text,
    api_key,
    max_sentences=1,
    min_sim=0.20,
    pad=0.3,
):
    """
    Robust semantic highlighter:
    - Uses embeddings to match answer_text to PDF sentences
    - Returns ONLY ONE rectangle
    - Rejects rectangles that are too large (>35% of the page)
    - If best match is huge, tries the next-best candidate
    """

    # ------------------------
    # SAFE LOCAL DEBUG LOGGER
    # ------------------------
    def _dbg_local(msg: str) -> None:
        try:
            from aqt import mw
            import os, time
            path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [semantic_hi] {msg}\n")
        except Exception:
            pass

    try:
        # DEBUG
        _dbg_local(f"DEBUG answer_text='{(answer_text or '')[:120]}'")
        _dbg_local(f"DEBUG words_count={len(words or [])}")

        # --------------------------------------------------------------------
        # 1. If title/caption filtering deletes everything, restore all words
        # --------------------------------------------------------------------
        usable = [
            w for w in (words or [])
            if not (w.get("is_title_line") or w.get("is_caption_line"))
        ]
        if not usable:
            usable = list(words or [])
            _dbg_local("DEBUG usable was empty after filtering → restored all words")

        if not usable:
            _dbg_local("No usable words or empty page → return []")
            return []

        if not (answer_text or "").strip():
            _dbg_local("Answer text empty → return []")
            return []

        # --------------------------------------------------------------------
        # 2. GROUP WORDS INTO SENTENCES (fallback to lines)
        # --------------------------------------------------------------------
        sentences = []
        current = []

        for idx, w in enumerate(usable):
            current.append(idx)
            if w.get("ends_sent"):
                text = " ".join(usable[j]["text"] for j in current).strip()
                if text:
                    sentences.append({"text": text, "idxs": current[:]})
                current = []

        if not sentences:
            # fallback to line groups
            line_groups = {}
            for i, w in enumerate(usable):
                line_groups.setdefault((w["block"], w["line"]), []).append(i)
            for _, idxs in line_groups.items():
                text = " ".join(usable[j]["text"] for j in idxs).strip()
                if text:
                    sentences.append({"text": text, "idxs": idxs[:]})

        if not sentences:
            _dbg_local("Sentence extraction produced 0 sentences → return []")
            return []

        _dbg_local(f"DEBUG sentences_count={len(sentences)}")

        # --------------------------------------------------------------------
        # 3. Embed answer + sentences
        # --------------------------------------------------------------------
        combined = [answer_text] + [s["text"] for s in sentences]
        try:
            embs = embed_texts(combined, api_key)
        except Exception as e:
            _dbg_local(f"Embedding error: {e}")
            embs = None

        # rank candidates
        if embs and len(embs) == len(combined):
            ans_emb = embs[0]
            sims = []

            for i, se in enumerate(embs[1:]):
                try:
                    sims.append((_cosine(ans_emb, se), i))
                except:
                    sims.append((-1.0, i))

            sims.sort(reverse=True, key=lambda x: x[0])
            ranked_idx = [idx for _, idx in sims]
            _dbg_local(f"DEBUG ranked_idx (embeddings)={ranked_idx}")

        else:
            # lexical fallback
            _dbg_local("Embeddings unavailable → fallback lexical matcher")
            atoks = set(re.findall(r"\w+", answer_text.lower()))
            scores = []

            for i, s in enumerate(sentences):
                stoks = set(re.findall(r"\w+", s["text"].lower()))
                overlap = len(atoks & stoks)
                scores.append((overlap, i))

            scores.sort(reverse=True, key=lambda x: x[0])
            ranked_idx = [idx for _, idx in scores]
            _dbg_local(f"DEBUG ranked_idx (lexical)={ranked_idx}")

        if not ranked_idx:
            _dbg_local("No ranked candidates → return []")
            return []

        # --------------------------------------------------------------------
        # 4. Try candidates IN ORDER, rejecting oversized rectangles.
        # --------------------------------------------------------------------
        # compute "page" bounds from usable words
        page_x0 = min(w["x0"] for w in usable)
        page_y0 = min(w["y0"] for w in usable)
        page_x1 = max(w["x1"] for w in usable)
        page_y1 = max(w["y1"] for w in usable)
        page_area = max(1e-6, (page_x1 - page_x0) * (page_y1 - page_y0))

        for idx in ranked_idx:
            sent = sentences[idx]

            xs, ys, xe, ye = [], [], [], []
            for wi in sent["idxs"]:
                w = usable[wi]
                xs.append(w["x0"]); ys.append(w["y0"])
                xe.append(w["x1"]); ye.append(w["y1"])

            if not xs:
                continue

            x0, y0 = min(xs), min(ys)
            x1, y1 = max(xe), max(ye)

            # tight rect
            rect = {
                "x": x0 - pad,
                "y": y0 - pad,
                "w": (x1 - x0) + 2 * pad,
                "h": (y1 - y0) + 2 * pad,
            }

            # compute rectangle area
            rect_area = (x1 - x0) * (y1 - y0)
            ratio = rect_area / page_area

            _dbg_local(f"DEBUG candidate idx={idx} rect_ratio={ratio:.3f}")

            # reject if too large
            if ratio > 0.35:
                _dbg_local("DEBUG → oversized rect rejected; trying next candidate")
                continue

            # ACCEPTED rectangle!
            _dbg_local("DEBUG → accepted rectangle")
            return [rect]

        # If we reach here, ALL rects were too large
        _dbg_local("All candidates were oversized → return []")
        return []

    except Exception as e:
        _dbg_local(f"FATAL semantic_sentence_rects: {e}")
        return []
# -------------------------------------------------------------------
# Compatibility stubs expected by main.py (kept minimal)
# -------------------------------------------------------------------

def boxes_for_phrase(words, phrase):
    return []

def sentence_rects_for_phrase(words, phrase, max_sentences=1, pad=0.3):
    return []

def extract_image_boxes(pdf_path: str, page_number: int) -> List[Dict]:
    """
    Placeholder preserved for compatibility with main.py.
    The semantic highlighter does not use image boxes yet.
    """
    return []