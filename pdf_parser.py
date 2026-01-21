
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
