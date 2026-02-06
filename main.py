
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

from aqt.qt import (
    QAction, QFileDialog, QInputDialog, QMessageBox, QDialog,
    QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QRadioButton,
    QSpinBox, QPushButton, QButtonGroup  # ← add this
)

from aqt.gui_hooks import add_cards_did_init
from aqt.addcards import AddCards

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
from .openai_cards import suggest_occlusions_from_image  # image detection API

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

    # Force EXACT Back template string
    desired_afmt = (

        "<hr id=answer>\n\n"
        "{{Back}}\n\n"
        "<br><br>\n\n"
        "{{SlideImage}}"

    )
    changed = False
    for t in (m.get("tmpls") or []):
        if t.get("afmt", "") != desired_afmt:
            t["afmt"] = desired_afmt
            changed = True
    if changed:
        col.models.save(m)

    return m

# ---------------------------------------------------------------------------
# IO editor: Get the current image bytes (original, DOM, or Fabric snapshot)
# ---------------------------------------------------------------------------
def _get_io_image_bytes_from_add_window(addw: AddCards) -> tuple[bytes, dict]:
    """
    Obtain the image currently shown in the native Image Occlusion editor.
    Tries, in order:
      A) Note['Image'] field: <img src="..."> OR plain filename
      B) DOM: #image-occlusion-container img OR first img in editor
      C) Fabric.js: background image or canvas.toDataURL({ multiplier: 2.0 })

    Returns:
      (bytes, meta)
      meta = {
        "origin": "original" | "snapshot",
        # when origin == "snapshot":
        "multiplier": float,
        # optional hints:
        "src": "field" | "dom" | "fabric-bg" | "fabric-snapshot"
      }
    """
    # --- get note safely ---
    try:
        note = addw.editor.note
    except Exception:
        note = None

    # ---------- A) From note field ----------
    try:
        if note:
            try:
                val = (note["Image"] or "").strip()
            except Exception:
                val = ""

            if val:
                src = ""
                m = re.search(r'src="([^"]+)"', val)
                if m:
                    src = m.group(1)
                else:
                    fn = re.sub(r"<[^>]*>", "", val).strip()
                    if fn.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")):
                        src = fn

                if src:
                    med_dir = addw.mw.col.media.dir()
                    path = os.path.join(med_dir, src)
                    if os.path.exists(path):
                        with open(path, "rb") as f:
                            data = f.read()
                            _dbg(f"IO-AI get_image: A(field) -> file '{src}' ({len(data)} bytes)")
                            return data, {"origin": "original", "src": "field"}
    except Exception as e:
        _dbg(f"IO-AI get_image: A(field) failed: {repr(e)}")

    # ---------- B) From DOM <img> ----------
    try:
        from PyQt6.QtCore import QEventLoop
        ans = {"src": None, "nw": 0, "nh": 0}
        loop = QEventLoop()

        def _cb(ret):
            ans.update(ret if isinstance(ret, dict) else {})
            loop.quit()

        js = r"""
        (function(){
          var el = document.querySelector('#image-occlusion-container img') || document.querySelector('img');
          if (!el) return ({ src: null, nw: 0, nh: 0 });
          var src = el.getAttribute('src');
          var nw = (el.naturalWidth  || el.width  || 0);
          var nh = (el.naturalHeight || el.height || 0);
          return ({ src: src, nw: nw, nh: nh });
        })();
        """
        addw.editor.web.evalWithCallback(
            f"(function(){{ return JSON.stringify({js}); }})()",
            lambda s: _cb(__import__('json').loads(s))
        )
        loop.exec()

        src = ans.get("src")
        dom_nw = int(ans.get("nw") or 0)
        dom_nh = int(ans.get("nh") or 0)

        if src:
            import base64
            if isinstance(src, str) and src.startswith("data:image/"):
                comma = src.find(",")
                if comma >= 0:
                    data = base64.b64decode(src[comma + 1:])
                    _dbg(f"IO-AI get_image: B(DOM) -> dataURL ({len(data)} bytes) natural={dom_nw}x{dom_nh}")
                    return data, {"origin": "original", "src": "dom", "domNaturalW": dom_nw, "domNaturalH": dom_nh}
            else:
                med_dir = addw.mw.col.media.dir()
                path = os.path.join(med_dir, src)
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        data = f.read()
                        _dbg(f"IO-AI get_image: B(DOM) -> file '{src}' ({len(data)} bytes) natural={dom_nw}x{dom_nh}")
                        return data, {"origin": "original", "src": "dom", "domNaturalW": dom_nw, "domNaturalH": dom_nh}
                else:
                    _dbg(f"IO-AI get_image: B(DOM) src '{src}' not found in media dir")
    except Exception as e:
        _dbg(f"IO-AI get_image: B(DOM) failed: {repr(e)}")

    # ---------- C) From Fabric.js (bg image or snapshot) ----------
    try:
        from PyQt6.QtCore import QEventLoop
        ans = {"val": None}
        loop = QEventLoop()

        def _cb(retval):
            ans["val"] = retval
            loop.quit()

        js = r"""
        (function(){
          try {
            var w = window;
            if (!w.fabric) return JSON.stringify({kind:'no-fabric'});
            // find fabric.Canvas
            var canv = w.__ioCanvas || w.canvas || w.fabricCanvas || null;
            if (!canv) {
              for (var k in w) {
                try {
                  if (w[k] && w[k].constructor && w[k].constructor.name === 'Canvas' && w[k]._objects) {
                    canv = w[k]; break;
                  }
                } catch(_e) {}
              }
            }
            if (!canv) return JSON.stringify({kind:'no-canvas'});

            // 1) Prefer background image source if present
            var bi = canv.backgroundImage && (canv.backgroundImage._element || canv.backgroundImage._originalElement);
            if (bi && bi.src) {
              return JSON.stringify({kind:'bg-src', val: bi.src});
            }

            // 2) Last resort: serialize visible canvas at 2x
            var dataURL = canv.toDataURL({ format: 'png', multiplier: 2.0 });
            return JSON.stringify({kind:'dataURL', val: dataURL, k: 2.0});
          } catch(e) {
            return JSON.stringify({kind:'err', val: String(e)});
          }
        })();
        """
        addw.editor.web.evalWithCallback(js, _cb)
        raw = ans["val"]
        if raw:
            import json, base64
            obj = json.loads(raw)
            kind = obj.get("kind")
            val = obj.get("val")

            if kind == "bg-src" and val:
                if isinstance(val, str) and val.startswith("data:image/"):
                    comma = val.find(",")
                    if comma >= 0:
                        data = base64.b64decode(val[comma + 1:])
                        _dbg(f"IO-AI get_image: C(Fabric) -> bg dataURL ({len(data)} bytes)")
                        return data, {"origin": "original", "src": "fabric-bg"}
                else:
                    med_dir = addw.mw.col.media.dir()
                    path = os.path.join(med_dir, val)
                    if os.path.exists(path):
                        with open(path, "rb") as f:
                            data = f.read()
                            _dbg(f"IO-AI get_image: C(Fabric) -> bg file '{val}' ({len(data)} bytes)")
                            return data, {"origin": "original", "src": "fabric-bg"}
                    else:
                        _dbg(f"IO-AI get_image: C(Fabric) bg-src '{val}' not found in media")

            if kind == "dataURL" and isinstance(val, str) and val.startswith("data:image/"):
                comma = val.find(",")
                if comma >= 0:
                    data = base64.b64decode(val[comma + 1:])
                    k = float(obj.get("k", 2.0) or 2.0)
                    _dbg(f"IO-AI get_image: C(Fabric) -> canv.toDataURL ({len(data)} bytes) k={k}")
                    return data, {"origin": "snapshot", "src": "fabric-snapshot", "multiplier": k}

            _dbg(f"IO-AI get_image: C(Fabric) returned kind={kind}")
    except Exception as e:
        _dbg(f"IO-AI get_image: C(Fabric) failed: {repr(e)}")

    _dbg("IO-AI get_image: all strategies failed")
    return b"", {}

# ---------------------------------------------------
# Utility: read image size from bytes (Qt / PIL)
# ---------------------------------------------------
def _image_size_from_bytes(img_bytes: bytes) -> tuple[int, int]:
    qimg = QImage.fromData(img_bytes)
    if not qimg.isNull():
        return int(qimg.width()), int(qimg.height())
    # (Optional) PIL fallback if you prefer
    try:
        from PIL import Image as PILImage  # type: ignore
        with PILImage.open(BytesIO(img_bytes)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return (0, 0)

# ---------------------------------------------------
# (Optional) Canvas snapshot if nothing else works
# ---------------------------------------------------
def _get_canvas_snapshot_bytes(addw: AddCards, multiplier: float = 2.0) -> tuple[bytes, float]:
    """Return (PNG bytes, multiplier) from Fabric canvas; empty bytes if not available."""
    from PyQt6.QtCore import QEventLoop
    hold = {"val": None}
    loop = QEventLoop()

    def _cb(val):
        hold["val"] = val
        loop.quit()

    js = rf"""
    (function(){{
      try {{
        var w = window;
        if (!w.fabric) return null;
        var canv = w.__ioCanvas || w.canvas || w.fabricCanvas || null;
        if (!canv) {{
          for (var k in w) {{
            try {{
              if (w[k] && w[k].constructor && w[k].constructor.name === 'Canvas' && w[k]._objects) {{
                canv = w[k]; break;
              }}
            }} catch(_e){{}}
          }}
        }}
        if (!canv) return null;
        return canv.toDataURL({{ format: 'png', multiplier: {multiplier:.4f} }});
      }} catch(e) {{ return null; }}
    }})();
    """
    addw.editor.web.evalWithCallback(js, _cb)
    data_url = hold["val"]
    if isinstance(data_url, str) and data_url.startswith("data:image/"):
        comma = data_url.find(",")
        if comma >= 0:
            import base64
            raw = base64.b64decode(data_url[comma+1:])
            _dbg(f"IO-AI: grabbed Fabric canvas snapshot x{multiplier} ({len(raw)} bytes)")
            return raw, multiplier
    _dbg("IO-AI: canvas snapshot unavailable")
    return b"", 1.0

# ---------------------------------------------------------------------------
# JS helper injected in IO editor: robust mapping + add rectangles
# ---------------------------------------------------------------------------
_JS_HELPER = r"""
(function(){
  if (window.__ioMapRectsAndAdd) return "ok";
  window.__ioMapRectsAndAdd = function(opts){
    try{
      var canv = window.__ioCanvas || window.canvas || window.fabricCanvas || null;
      if (!canv || !window.fabric) return JSON.stringify({ok:false, err:"no-fabric-canvas"});
      var rects = opts.rects || [];
      var origin = String(opts.origin||"original").toLowerCase(); // 'original' | 'snapshot'
      var aiW = +opts.aiW || 0, aiH = +opts.aiH || 0;
      var multiplier = +opts.multiplier || 1.0;

      var r = (typeof canv.getRetinaScaling === 'function') ? canv.getRetinaScaling() : (window.devicePixelRatio || 1);
      var vpt = canv.viewportTransform || [1,0,0,1,0,0];
      var vptInv = fabric.util.invertTransform(vpt);

      // Resolve bg viewport rectangle + its space
      var bgV = null, bgSpace = 'S';
      var bi = canv.backgroundImage;
      try {
        if (bi && typeof bi.getBoundingRect === 'function') {
          var br = bi.getBoundingRect(true, true); // post-viewport, CANVAS px
          if (br && br.width && br.height) {
            bgV = {left:br.left, top:br.top, width:br.width, height:br.height};
            bgSpace = 'C';
          }
        }
      } catch(_e){}
      if (!bgV) {
        // DOM fallback -> CSS px
        var imgEl = document.querySelector('#image-occlusion-container img') || document.querySelector('img');
        var cavEl = canv.lowerCanvasEl || canv.upperCanvasEl || document.querySelector('canvas');
        if (imgEl && cavEl) {
          var ri = imgEl.getBoundingClientRect(), rc = cavEl.getBoundingClientRect();
          var left = Math.max(ri.left, rc.left), top = Math.max(ri.top, rc.top);
          var right = Math.min(ri.right, rc.right), bottom = Math.min(ri.bottom, rc.bottom);
          var wInt = Math.max(0, right - left), hInt = Math.max(0, bottom - top);
          if (wInt > 0 && hInt > 0) {
            bgV = { left: left - rc.left, top: top - rc.top, width: wInt, height: hInt };
            bgSpace = 'S';
          }
        }
      }

      function S2C(x, y){ return (bgSpace==='S') ? {x:x*r, y:y*r} : {x:x, y:y}; }
      function C2O(x, y){
        var p = fabric.util.transformPoint(new fabric.Point(x, y), vptInv);
        return {x:p.x, y:p.y};
      }

      function addRectO(xO, yO, wO, hO){
        var rect = new fabric.Rect({
          left: xO, top: yO, width: wO, height: hO,
          fill: 'rgba(255,235,162,1)', stroke: '#212121', strokeWidth: 1,
          selectable: true, hasControls: true
        });
        canv.add(rect);
        return rect;
      }

      var added = 0, mappedPreview = null;

      if (origin === 'snapshot') {
        // SNAPSHOT: D(px) -> divide by multiplier -> C(px) -> O
        for (var i=0;i<rects.length;i++){
          var R = rects[i];
          var xC = (+R.x) / (multiplier || 1);
          var yC = (+R.y) / (multiplier || 1);
          var wC = (+R.w) / (multiplier || 1);
          var hC = (+R.h) / (multiplier || 1);

          var tlO = C2O(xC, yC);
          var brO = C2O(xC + wC, yC + hC);
          var xO = tlO.x, yO = tlO.y, wO = (brO.x - tlO.x), hO = (brO.y - tlO.y);
          if (wO > 1 && hO > 1) { addRectO(xO, yO, wO, hO); added++; if (!mappedPreview) mappedPreview = {xO,yO,wO,hO}; }
        }
      } else {
        // ORIGINAL: I(px) -> bg viewport (S or C) -> (if S)*r -> C -> O
        if (!bgV || !bgV.width || !bgV.height || !aiW || !aiH) {
          return JSON.stringify({ok:false, err:"bgV/ai size missing", meta:{bgV, aiW, aiH}});
        }
        var sx = bgV.width / aiW, sy = bgV.height / aiH;
        for (var i=0;i<rects.length;i++){
          var R = rects[i];
          var xS = bgV.left + (+R.x) * sx;
          var yS = bgV.top  + (+R.y) * sy;
          var wS = (+R.w)   * sx;
          var hS = (+R.h)   * sy;

          var tlC = S2C(xS, yS);
          var brC = S2C(xS + wS, yS + hS);
          var tlO = C2O(tlC.x, tlC.y);
          var brO = C2O(brC.x, brC.y);

          var xO = tlO.x, yO = tlO.y, wO = (brO.x - tlO.x), hO = (brO.y - tlO.y);
          if (wO > 1 && hO > 1) { addRectO(xO, yO, wO, hO); added++; if (!mappedPreview) mappedPreview = {xO,yO,wO,hO}; }
        }
      }

      canv.renderAll();

      // Forward-projection debug of the first mapped rect (O->C->S)
      var rt = null;
      if (mappedPreview){
        var a=vpt[0]||1, d=vpt[3]||1, e=vpt[4]||0, f=vpt[5]||0;
        var xC = a*mappedPreview.xO + e, yC = d*mappedPreview.yO + f;
        var xS = xC / r, yS = yC / r;
        rt = {O:{x:mappedPreview.xO,y:mappedPreview.yO}, C:{x:xC,y:yC}, S:{x:xS,y:yS}};
      }

      return JSON.stringify({ok:true, added, meta:{bgSpace, r, vpt, bgV}, rt});
    } catch(err) {
      return JSON.stringify({ok:false, err:String(err)});
    }
  };
  return "ok";
})();
"""

# ---------------------------------------------------------------------------
# IO action: detect masks, call JS helper to map+inject, show result
# ---------------------------------------------------------------------------
def _io_auto_occlude_action(addw: AddCards):
    """Auto-detect masks and add them to the native IO editor, aligned correctly."""
    # --- Preconditions --------------------------------------------------------
    m = addw.editor.note.model()
    if not m or m.get("name", "") != "Image Occlusion":
        showWarning("Switch note type to 'Image Occlusion' first.")
        return

    api_key = get_api_key()
    if not api_key:
        return

    from PyQt6.QtCore import QEventLoop

    # --- 1) Get the AI source image ------------------------------------------
    got = _get_io_image_bytes_from_add_window(addw)
    if isinstance(got, tuple) and len(got) == 2:
        img_bytes, ai_meta = got
    else:
        img_bytes, ai_meta = got or b"", {}

    origin = (ai_meta or {}).get("origin", "original")  # "snapshot" | "original"
    snapshot_multiplier_used = float((ai_meta or {}).get("multiplier", 1.0) or 1.0)

    if not img_bytes:
        # Fallback: 2× Fabric snapshot
        snap_bytes, k = _get_canvas_snapshot_bytes(addw, multiplier=2.0)
        if snap_bytes:
            img_bytes = snap_bytes
            origin = "snapshot"
            snapshot_multiplier_used = k
            _dbg(f"IO-AI: using canvas snapshot for AI (k={k}).")
        else:
            showWarning("No image found in the Image Occlusion editor.")
            return

    ai_w, ai_h = _image_size_from_bytes(img_bytes)
    if ai_w <= 0 or ai_h <= 0:
        showWarning("Could not read the IO image dimensions.")
        return
    _dbg(f"IO-AI: AI source image size = {ai_w}x{ai_h}, origin={origin}, k={snapshot_multiplier_used}")

    # --- 2) Ask OpenAI for rectangles ----------------------------------------
    try:
        mw.progress.start(label="Analyzing image (AI masks)…", immediate=True)
        out = suggest_occlusions_from_image(img_bytes, api_key, max_masks=12, temperature=0.0)
    finally:
        try: mw.progress.finish()
        except Exception: pass

    masks = (out.get("masks") if isinstance(out, dict) else []) or []
    _dbg(f"IO-AI: masks_raw={len(masks)}")
    if not masks:
        tooltip("No label-like regions detected.")
        return

    # --- 3) Filter small / overlapping masks ---------------------------------
    def _nms_filter(rects, iou_thr=0.50):
        def _iou(a, b):
            ax1, ay1, ax2, ay2 = a["x"], a["y"], a["x"]+a["w"], a["y"]+a["h"]
            bx1, by1, bx2, by2 = b["x"], b["y"], b["x"]+b["w"], b["y"]+b["h"]
            ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
            iw, ih = max(0, ix2-ix1), max(0, iy2-iy1)
            inter = iw*ih
            if inter == 0:
                return 0.0
            area_a = a["w"]*a["h"]; area_b = b["w"]*b["h"]
            return inter / (area_a + area_b - inter + 1e-9)

        rects = sorted(rects, key=lambda r: r["w"]*r["h"], reverse=True)
        kept = []
        for r in rects:
            if all(_iou(r, k) < iou_thr for k in kept):
                kept.append(r)
        return kept

    MIN_W, MIN_H, MIN_AREA = 14, 10, 150
    masks = [m for m in masks
             if int(m.get("w", 0)) >= MIN_W
             and int(m.get("h", 0)) >= MIN_H
             and (int(m.get("w", 0)) * int(m.get("h", 0))) >= MIN_AREA]
    masks = _nms_filter(masks, iou_thr=0.50)
    _dbg(f"IO-AI: masks_after_filter={len(masks)}")
    if not masks:
        tooltip("AI found only tiny/overlapping regions; nothing to add.")
        return

    # --- 4) Inject JS helper (once) ------------------------------------------
    injected = {"ok": False}
    loop = QEventLoop()
    addw.editor.web.evalWithCallback(_JS_HELPER, lambda _: (injected.__setitem__("ok", True), loop.quit()))
    loop.exec()

    # --- 5) Call the helper to map+add ---------------------------------------
    import json
    payload = {
        "origin": origin,                # 'original' or 'snapshot'
        "rects": masks,                  # [{x,y,w,h}, ...] in AI space
        "aiW": int(ai_w),
        "aiH": int(ai_h),
        "multiplier": float(snapshot_multiplier_used or 1.0),
    }
    js_call = f"""
    (function(p){{
      try {{
        var o = JSON.parse(p);
        if (!window.__ioMapRectsAndAdd) return JSON.stringify({{"ok":false,"err":"helper-missing"}});
        return window.__ioMapRectsAndAdd(o);
      }} catch(e) {{
        return JSON.stringify({{"ok":false,"err":String(e)}});
      }}
    }})({json.dumps(json.dumps(payload))});
    """

    res_hold = {"val": "{}"}
    loop = QEventLoop()
    addw.editor.web.evalWithCallback(js_call, lambda v: (res_hold.__setitem__("val", v or "{}"), loop.quit()))
    loop.exec()

    try:
        res = json.loads(res_hold["val"])
    except Exception:
        res = {"ok": False, "err": "json-parse"}

    if not res.get("ok"):
        _dbg(f"IO-AI JS mapper failed: {res}")
        showWarning(f"Auto‑Occlude mapping failed: {res.get('err')}")
        return

    added = int(res.get("added", 0))
    meta  = res.get("meta", {})
    rt    = res.get("rt", None)
    _dbg(f"IO-AI JS mapped+added: added={added} bgSpace={meta.get('bgSpace')} retina={meta.get('r')} vpt={meta.get('vpt')} bgV={meta.get('bgV')}")
    if rt:
        _dbg(f"IO-AI RT check: O({rt['O']['x']:.2f},{rt['O']['y']:.2f}) -> C({rt['C']['x']:.2f},{rt['C']['y']:.2f}) -> S({rt['S']['x']:.2f},{rt['S']['y']:.2f})")

    tooltip(f"Added {added} mask(s). Adjust as needed, then click Add.")

# ---------------------------------------------------------------------------
# Install Auto‑Occlude (AI) button
# ---------------------------------------------------------------------------
def _install_io_button_on_add_window(addw: AddCards):
    """Install the Auto‑Occlude (AI) button in the Add window's button bar."""
    try:
        from aqt.qt import QPushButton, QDialogButtonBox

        btn = QPushButton("Auto-Occlude (AI)", addw)
        btn.setToolTip("Auto-detect label rectangles (OpenAI) and add as masks")

        def _clicked():
            try:
                _io_auto_occlude_action(addw)
            except Exception as e:
                _dbg("IO-AI button error: " + repr(e))
                _dbg(traceback.format_exc())
                showWarning(f"Auto‑Occlude failed:\n{e}")

        btn.clicked.connect(_clicked)
        addw.form.buttonBox.addButton(btn, QDialogButtonBox.ButtonRole.ActionRole)
        _dbg("IO-AI: button installed")
    except Exception as e:
        _dbg("IO-AI: failed to install button: " + repr(e))

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


# -------------------------------
# Optional image masking demo (Basic cards)
# -------------------------------
def _draw_rect_mask_qt(png_bytes: bytes, x: int, y: int, w: int, h: int) -> bytes:
    """Mask a rectangle using Qt only (no Pillow). Returns PNG bytes."""
    img = QImage.fromData(png_bytes)
    if img.isNull():
        return png_bytes
    painter = QPainter(img)
    painter.setPen(QColor(160, 160, 160))
    painter.setBrush(QColor(242, 242, 242))
    painter.drawRect(QRect(x, y, w, h))
    painter.end()
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(ba)

def _draw_rect_mask(img: "Image.Image", x: int, y: int, w: int, h: int,
                    fill=(242, 242, 242), outline=(160, 160, 160)) -> "Image.Image":
    """Return a COPY of img with a light rectangle mask (friendly in dark mode)."""
    try:
        from PIL import ImageDraw  # type: ignore
    except Exception:
        return img
    out = img.copy()
    drw = ImageDraw.Draw(out)
    drw.rectangle([x, y, x + w, y + h], fill=fill, outline=outline, width=2)
    return out

# -------------------------------
# Fallback fabric injection (not used by JS helper path)
# -------------------------------
def _inject_io_rectangles(addw: AddCards, rects: list[dict]) -> str:
    """(Unused in JS-mode) Fabric-js fallback injector. Kept for completeness."""
    import json
    rects_js = json.dumps([{
        "x": int(r["x"]), "y": int(r["y"]),
        "w": int(r["w"]), "h": int(r["h"])
    } for r in rects])

    js = f"""
    (function(rects){{
      try {{
        var w = window;
        if (!w.fabric) return 'no-fabric';
        var canv = w.__ioCanvas || w.canvas || w.fabricCanvas || null;
        if (!canv) {{
          for (var k in w) {{
            try {{
              if (w[k] && w[k].constructor && w[k].constructor.name === 'Canvas' && w[k]._objects) {{
                canv = w[k]; break;
              }}
            }} catch(_e) {{}}
          }}
        }}
        if (!canv) return 'no-canvas';
        rects.forEach(function(r){{
          var rect = new w.fabric.Rect({{
            left: r.x, top: r.y, width: r.w, height: r.h,
            fill: 'rgba(255,235,162,1)',
            stroke: '#212121', strokeWidth: 1,
            selectable: true, hasControls: true
          }});
          canv.add(rect);
        }});
        canv.renderAll();
        return 'ok:fabric';
      }} catch(e) {{
        return 'err:' + e;
      }}
    }})(JSON.parse({json.dumps(rects_js)}));
    """
    from PyQt6.QtCore import QEventLoop
    result = {"val": "err:eval"}
    loop = QEventLoop()
    addw.editor.web.evalWithCallback(js, lambda v: (result.__setitem__("val", v or "ok"), loop.quit()))
    loop.exec()
    return result["val"]

# -------------------------------
# Standalone auto image occlusion to Basic
# -------------------------------
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

def auto_image_occlusion_ai():
    """
    Auto Image Occlusion (AI)
    - User picks an image
    - Base image saved (unmodified)
    - AI rectangles -> <shape .../> items wrapped as clozes in Occlusion
    """
    # 1) API key
    api_key = get_api_key()
    if not api_key:
        return

    # 2) Image file
    img_path, _ = QFileDialog.getOpenFileName(
        mw, "Select image for auto-occlusion", "", "Images (*.png *.jpg *.jpeg *.gif)"
    )
    if not img_path:
        return

    # 3) Normalize to PNG + save base image
    def _normalize_to_png_bytes_qt(path: str, max_width: int = 1600) -> Optional[bytes]:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            return None
        img = QImage.fromData(raw)
        if img.isNull():
            return None
        if img.width() > max_width:
            img = img.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return bytes(ba)

    png_bytes = _normalize_to_png_bytes_qt(img_path)
    if not png_bytes:
        showWarning("Could not load or normalize image.")
        return

    base = os.path.splitext(os.path.basename(img_path))[0]
    base_media_name = f"{base}_base.png"
    base_media = _write_media_file(base_media_name, png_bytes)
    deck_name = base.strip() or "Image Occlusions"
    deck_id = get_or_create_deck(deck_name)

    if not base_media:
        showWarning("Failed to store base image in media.")
        return

    # 4) Ask LLM for rectangles
    try:
        mw.progress.start(label="Analyzing image (AI occlusions)…", immediate=True)
        out = suggest_occlusions_from_image(png_bytes, api_key)
    finally:
        try:
            mw.progress.finish()
        except Exception:
            pass

    masks = out.get("masks", []) if isinstance(out, dict) else []
    if not masks:
        showWarning("No label-like regions detected.")
        return

    # Make Basic notes with masked variants (demo)
    from anki.notes import Note
    model = get_basic_model_fallback() or mw.col.models.byName("Basic")
    if not model:
        model = ensure_basic_with_slideimage("Basic + Slide")

    created = 0
    for i, m in enumerate(masks, start=1):
        x = int(m["x"]); y = int(m["y"]); w = int(m["w"]); h = int(m["h"])
        masked_png = _draw_rect_mask_qt(png_bytes, x, y, w, h)
        masked_name = f"{base}_mask_{i}.png"
        masked_media = _write_media_file(masked_name, masked_png)
        if not masked_media:
            _dbg(f"Failed to write mask {i} to media.")
            continue

        note = Note(mw.col, model)
        note.did = deck_id
        note["Front"] = f'<img src="{os.path.basename(masked_media)}">'
        note["Back"] = ""
        note.tags.append(f"auto-occlusion::{base}")
        mw.col.addNote(note)
        try:
            cids = [c.id for c in note.cards()]
            force_move_cards_to_deck(cids, deck_id)
        except:
            pass
        created += 1

    mw.col.save()
    _dbg(f"IO-AI BASIC MODE: created {created} occlusion notes from {len(masks)} rectangles.")
    showInfo(f"Created {created} AI occlusion cards.", title="Auto Image Occlusion (AI)")

# -------------------------------
# Ensure Cloze + Slide model
# -------------------------------
def ensure_image_occlusion_ai(model_name: str = "Image Occlusion (AI)") -> dict:
    col = mw.col
    m = col.models.byName(model_name)
    created = False
    if not m:
        m = col.models.new(model_name)
        created = True

    if m.get("type", 0) != 1:
        m["type"] = 1  # CLOZE

    want_fields = ["Header", "Image", "Occlusion", "Back Extra"]
    have_names = [f.get("name") for f in (m.get("flds") or [])]
    for name in want_fields:
        if name not in have_names:
            fld = col.models.newField(name)
            col.models.addField(m, fld)

    front = """
{{#Header}}<div>{{Header}}</div>{{/Header}}
<div style="display: none">{{cloze:Occlusion}}</div>
<div id="err"></div>

<div id="image-occlusion-container">
    {{Image}}
    <canvas id="image-occlusion-canvas"></canvas>
</div>

<script>
try {
    anki.imageOcclusion.setup();
} catch (exc) {
    document.getElementById("err").innerHTML = `Error loading image occlusion.<br><br>${exc}`;
}
</script>
""".strip()

    back = """
{{#Header}}<div>{{Header}}</div>{{/Header}}
<div style="display: none">{{cloze:Occlusion}}</div>
<div id="err"></div>

<div id="image-occlusion-container">
    {{Image}}
    <canvas id="image-occlusion-canvas"></canvas>
</div>

<script>
try {
    anki.imageOcclusion.setup();
} catch (exc) {
    document.getElementById("err").innerHTML = `Error loading image occlusion.<br><br>${exc}`;
}
</script>

<div><button id="toggle">Toggle Masks</button></div>
{{#Back Extra}}<div>{{Back Extra}}</div>{{/Back Extra}}
""".strip()

    css = """
#image-occlusion-canvas {
    --inactive-shape-color: #ffeba2;
    --active-shape-color: #ff8e8e;
    --inactive-shape-border: 1px #212121;
    --active-shape-border: 1px #212121;
    --highlight-shape-color: #ff8e8e00;
    --highlight-shape-border: 1px #ff8e8e;
}
.card { font-family: arial; font-size: 20px; text-align: center; color: black; background-color: white; }
.card img { max-width: 100%; height: auto; }
""".strip()

    tmpls = m.get("tmpls") or []
    if not tmpls:
        t = col.models.newTemplate("Card 1")
        t["qfmt"] = front
        t["afmt"] = back
        col.models.addTemplate(m, t)
    else:
        t = tmpls[0]
        changed = False
        if t.get("qfmt") != front:
            t["qfmt"] = front; changed = True
        if t.get("afmt") != back:
            t["afmt"] = back; changed = True
        if changed:
            m["tmpls"][0] = t

    if m.get("css") != css:
        m["css"] = css

    if created:
        col.models.add(m)
    else:
        col.models.save(m)
    return col.models.byName(model_name)

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
        t["qfmt"] = "{{cloze:Text}}"
        t["afmt"] = (
            "{{cloze:Text}}\n\n"
            "{{Back Extra}}\n\n"
            "<img src={{SlideImage}}>"
        )
        col.models.addTemplate(m, t)
        col.models.add(m)

    names = [f.get("name") for f in (m.get("flds") or [])]
    if "SlideImage" not in names:
        fld = col.models.newField("SlideImage")
        col.models.addField(m, fld)

    desired_qfmt = "{{cloze:Text}}"
    desired_afmt = (
        "{{cloze:Text}}\n\n"
        "{{Back Extra}}\n\n"
        "{{SlideImage}}"
    )
    changed = False
    for t in (m.get("tmpls") or []):
        if t.get("qfmt", "") != desired_qfmt:
            t["qfmt"] = desired_qfmt; changed = True
        if t.get("afmt", "") != desired_afmt:
            t["afmt"] = desired_afmt; changed = True
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

        h_pages.addWidget(QLabel("From:"))
        h_pages.addWidget(self.spin_page_from)
        h_pages.addWidget(QLabel("To:"))
        h_pages.addWidget(self.spin_page_to)
        v.addLayout(h_pages)

        def _sync_pages_enabled():
            enabled = self.rb_pages_range.isChecked()
            self.spin_page_from.setEnabled(enabled)
            self.spin_page_to.setEnabled(enabled)

        self.rb_pages_all.toggled.connect(_sync_pages_enabled)
        self.rb_pages_range.toggled.connect(_sync_pages_enabled)
        _sync_pages_enabled()

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
        c["types_basic"] = self.chk_basic.isChecked()
        c["types_cloze"] = self.chk_cloze.isChecked()
        c["per_slide_mode"] = mode
        c["per_slide_min"] = minv
        c["per_slide_max"] = maxv
        c["color_after_generation"] = self.chk_color_after.isChecked()
        c["ai_extend_color_table"] = self.chk_ai_extend_colors.isChecked()
        _save_config(c)

        return {
            "types_basic": self.chk_basic.isChecked(),
            "types_cloze": self.chk_cloze.isChecked(),
            "per_slide_mode": mode,
            "per_slide_min": minv,
            "per_slide_max": maxv,
            "page_mode": page_mode,
            "page_from": page_from,
            "page_to": page_to,
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
            mw.taskman.run_on_main(lambda i=idx, t=total_pages: ui_update(f"Processing page {i} of {t}"))

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

            if mode == "range":
                if cards:
                    n = max(0, min(random.randint(minv, maxv), len(cards)))
                    cloze_first, non_cloze = [], []
                    for c in cards:
                        f = (c.get("front") or ""); b = (c.get("back") or "")
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

    if not isinstance(result, dict) or not result.get("ok", False):
        tb = result.get("traceback", "") if isinstance(result, dict) else ""
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
        return

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
                        safe_deck = re.sub(r"[^A-Za-z0-9_-]+", "_", deck_name)
                        suggested = f"{safe_deck}_{base}_p{p}.png"

                        stored = _write_media_file(suggested, data)
                        page_to_media[p] = stored   # ✅ USE WHAT ANKI ACTUALLY STORED
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

    if want_basic:
        models["basic"] = ensure_basic_with_slideimage(models.get("basic", {}).get("name", "Basic + Slide"))
    if want_cloze:
        models["cloze"] = ensure_cloze_with_slideimage(models.get("cloze", {}).get("name", "Cloze + Slide"))

    mw.progress.start(label=f"Inserting {len(cards)} card(s)…", immediate=True)
    try:
        from os.path import basename
        for idx, (front, back, page_no) in enumerate(cards, start=1):
            media_name = page_to_media.get(page_no)
            fname = basename(media_name) if media_name else ""

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
                if is_cloze_front:
                    text_val, back_extra = front, back
                else:
                    text_val, back_extra = back, front
                if not text_val.strip():
                    _dbg("SKIP: empty cloze Text")
                    continue
                _dbg(f"CLOZE CHOSEN text_len={len(text_val)} back_extra_len={len(back_extra)} preview='{text_val[:120]}'")
                note["Text"] = text_val
                note["Back Extra"] = back_extra
                if "SlideImage" in note:
                    note["SlideImage"] = f'<img src="{fname}">'
                note.tags.append("pdf2cards:ai_cloze")
                col.addNote(note)
                try:
                    cids = [c.id for c in note.cards()]
                    force_move_cards_to_deck(cids, deck_id)
                except Exception:
                    pass
                continue

            if want_basic:
                model = models["basic"]
                col.models.set_current(model)
                note = col.newNote()
                note.did = deck_id
                note["Front"] = front
                note["Back"]  = back
                if "SlideImage" in note:
                    note["SlideImage"] = f'<img src="{fname}">'
                note.tags.append("pdf2cards:basic")
                col.addNote(note)
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

    # ======================================================
    # Auto-color newly generated deck (SAFE TASKMAN VERSION)
    # ======================================================
    try:
        cfg = _get_config()
        if cfg.get("color_after_generation", True):

            def _collect_nids():
                # BACKGROUND THREAD (read-only)
                from aqt import mw
                cids = mw.col.find_cards(f"deck:{deck_name}")
                return list({mw.col.get_card(cid).nid for cid in cids})

            def _extend_colors_bg(nids):
                # BACKGROUND THREAD (API + pure Python only)
                try:
                    cfg = _get_config()
                    if not cfg.get("ai_extend_color_table", True):
                        return

                    from .colorizer import get_entries_for_editor, set_color_table_entries
                    from .openai_cards import generate_color_table_entries

                    api_key = cfg.get("openai_api_key", "").strip()
                    if not api_key:
                        return

                    import re

                    text_blob = []
                    cloze_terms = []

                    CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}")

                    for front, back, _ in cards:
                        for txt in (front, back):
                            if not txt:
                                continue

                            # 1️⃣ Always include full text
                            text_blob.append(txt)

                            # 2️⃣ Collect cloze terms as priority signals
                            for m in CLOZE_RE.finditer(txt):
                                term = m.group(1).strip()
                                if not term:
                                    continue
                                for part in re.split(r"[;,]", term):
                                    p = part.strip()
                                    if p:
                                        cloze_terms.append(p)

                    # 3️⃣ Hybrid source text: full text + emphasized clozes
                    source_text = (
                        "\n".join(text_blob)
                        + "\n\nIMPORTANT TERMS (high priority):\n"
                        + "\n".join(cloze_terms)
                    )[:8000]

                    _dbg(
                        f"AI color-table using HYBRID input "
                        f"(full_text_chars={len(' '.join(text_blob))}, "
                        f"cloze_terms={len(cloze_terms)})"
                    )
                    
                    existing = get_entries_for_editor()
                    existing = existing[:200]  # ⬅️ ADD THIS LINE

                    existing_words = {
                        (e.get("word") or "").strip().lower()
                        for e in existing
                    }

                    # ✅ Append-only AI call
                    new_entries = generate_color_table_entries(
                        source_text=source_text,
                        existing_entries=existing,
                        deck_hint=deck_name,
                        api_key=api_key,
                    )

                    if not new_entries:
                        _dbg(f"AI color-table: no new entries for deck '{deck_name}'")
                        return

                    merged = existing[:]
                    added = []

                    for e in new_entries:
                        w = (e.get("word") or "").strip().lower()
                        if w and w not in existing_words:
                            merged.append(e)
                            added.append(e)

                    if not added:
                        _dbg(f"AI color-table: nothing new to add for deck '{deck_name}'")
                        return

                    set_color_table_entries(merged)

                    # ✅ Clear logging (like before)
                    _dbg(
                        f"AI color-table: added {len(added)} entries to deck '{deck_name}'"
                    )
                    for e in added:
                        _dbg(
                            f"  + {e.get('word')} "
                            f"(group={e.get('group','')}, color={e.get('color','')})"
                        )

                except Exception as e:
                    _dbg("AI color-table extension failed: " + repr(e))

            def _apply_color():
                # MAIN THREAD (write)
                try:
                    from .colorizer import apply_to_deck_ids
                    apply_to_deck_ids([deck_id])
                except Exception as e:
                    _dbg("Auto-color failed: " + repr(e))

            def _on_collected(fut):
                try:
                    nids = fut.result()

                    # 1️⃣ Run AI extension in background
                    def _after_ai(_):
                        # 2️⃣ Apply coloring on main thread
                        mw.taskman.run_on_main(_apply_color)

                    mw.taskman.run_in_background(
                        lambda: _extend_colors_bg(nids),
                        on_done=_after_ai
                    )

                except Exception as e:
                    _dbg("Auto-color failed: " + repr(e))

            # Let Anki fully settle, then start pipeline
            mw.taskman.run_in_background(_collect_nids, on_done=_on_collected)
    except Exception as e:
        _dbg("Auto-color scheduling failed: " + repr(e))

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

def _on_add_init(addw: AddCards):
    # Called once per Add window open
    _install_io_button_on_add_window(addw)

def _on_add_show(addw: AddCards, _):
    # (kept for possible future UI toggles)
    try:
        is_io = addw.editor.note and addw.editor.note.model().get("name","") == "Image Occlusion"
        for b in addw.form.buttonBox.buttons():
            if b.text() == "Auto‑Occlude (AI)":
                b.setVisible(bool(is_io))
    except Exception:
        pass

def init_addon():
    action = QAction("Generate Anki cards from PDF", mw)
    action.triggered.connect(generate_from_pdf)
    mw.form.menuTools.addAction(action)

    diag = QAction("Check PDF Renderer (automatic-anki)", mw)
    diag.triggered.connect(check_pdf_renderer)
    mw.form.menuTools.addAction(diag)

    io_action = QAction("Auto Image Occlusion (AI)", mw)
    io_action.triggered.connect(auto_image_occlusion_ai)
    mw.form.menuTools.addAction(io_action)

    add_cards_did_init.append(_on_add_init)
