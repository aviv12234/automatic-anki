
# local no-op/debug logger to avoid circular import
def _dbg(msg: str) -> None:
    try:
        from aqt import mw
        import os, time
        path = os.path.join(mw.pm.profileFolder(), "pdf2cards_debug.log")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        # Fall back to a silent no-op if we can't log
        pass
# openai_cards.py

import json
import requests
from typing import Dict


# --- OCR helper (OpenAI Vision) ---
import base64
import requests

OPENAI_MODEL = "gpt-4o-mini"

def _limit_png_size_for_vision(png_bytes: bytes, max_bytes: int = 3_500_000) -> bytes:
    # If your PNG is too big, Vision sometimes returns empty output.
    if len(png_bytes) <= max_bytes:
        return png_bytes
    try:
        from PyQt6.QtGui import QImage
        from PyQt6.QtCore import QByteArray, QBuffer, QIODevice
        img = QImage.fromData(png_bytes)
        if img.isNull():
            return png_bytes
        # scale down by 0.75 until under max_bytes (simple loop)
        scale = 0.85
        w, h = img.width(), img.height()
        for _ in range(6):
            w = max(1, int(w * scale))
            h = max(1, int(h * scale))
            small = img.scaled(w, h)
            ba = QByteArray()
            buf = QBuffer(ba); buf.open(QIODevice.OpenModeFlag.WriteOnly)
            small.save(buf, b"PNG")
            buf.close()
            out = bytes(ba)
            if len(out) <= max_bytes:
                return out
            img = small
        return out
    except Exception:
        return png_bytes
    

def ocr_page_image(image_bytes: bytes, api_key: str) -> str:
    # Log: we want to see this in pdf2cards_debug.log
    _dbg(f"OCR CALL: bytes={len(image_bytes) if image_bytes else 0}")

    if not image_bytes or not api_key:
        _dbg("OCR ABORT: missing image or API key")
        return ""

    import base64, requests
    b64 = base64.b64encode(image_bytes).decode("ascii")

    payload = {
        "model": "gpt-4o-mini",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Extract all text. Plain text only."},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                ],
            }
        ],
    }

    try:
        resp = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        _dbg(f"OCR HTTP: status={resp.status_code}")
        resp.raise_for_status()
        data = resp.json()
        text = (
            data.get("output", [{}])[0]
                .get("content", [{}])[0]
                .get("text", "")
                .strip()
        )
        _dbg(f"OCR OK: {len(text)} chars")
        return text
    except Exception as e:
        _dbg(f"OCR ERROR: {repr(e)}")
        return ""
    


OPENAI_MODEL   = "gpt-4o-mini"
TEMPERATURE    = 0.2
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"


# --- Vision occlusion suggester (minimal, rectangles only) -------------------
import base64
from typing import Dict, Any
# Reuse your existing OPENAI_API_URL / OPENAI_MODEL constants.
OPENAI_VISION_MODEL = OPENAI_MODEL  # "gpt-4o-mini"

SYSTEM_PROMPT_OCCLUSION = """
You are a vision assistant that detects small, high-yield text labels in a study image
(diagrams, charts, anatomy, tables). Return 1–16 rectangular regions that tightly bound
individual labels or short phrases (NOT whole sentences).

Return JSON ONLY with this exact schema (no extra keys):
{
  "masks": [
    {"x": 100, "y": 230, "w": 220, "h": 60}
  ]
}

Rules:
- x,y = top-left; w,h = width,height; all integers.
- Coordinates MUST be within the image bounds (0..width-1, 0..height-1).
- Avoid huge boxes; prefer tight boxes around text/labels.
- Do not overlap excessively; skip duplicates.
- If nothing appropriate is found, return { "masks": [] }.
""".strip()

def suggest_occlusions_from_image(
    image_bytes: bytes, api_key: str, max_masks: int = 16, temperature: float = 0.1
) -> Dict[str, Any]:
    """
    Ask the LLM to propose rectangular occlusions (no labels).
    Returns {"masks": [ {x:int, y:int, w:int, h:int}, ... ]}.
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    user_content = [
        {"type": "text", "text": f"Detect up to {max_masks} tight label rectangles."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
    ]
    resp = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_VISION_MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT_OCCLUSION},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    try:
        data = json.loads(content)
        masks = data.get("masks", [])
        if not isinstance(masks, list):
            return {"masks": []}
        out = []
        for m in masks[:max_masks]:
            try:
                out.append({
                    "x": int(m.get("x", 0)),
                    "y": int(m.get("y", 0)),
                    "w": int(m.get("w", 0)),
                    "h": int(m.get("h", 0)),
                })
            except Exception:
                continue
        # filter invalid
        return {"masks": [m for m in out if m["w"] > 0 and m["h"] > 0]}
    except json.JSONDecodeError:
        return {"masks": []}


# openai_cards.py
# (only showing the parts to change)

SYSTEM_PROMPT_BASIC = """
You are an expert study-card writer for science/medicine PDFs.

TASK:
Generate ONLY BASIC Anki cards (question → answer).

DO NOT MAKE CARDS ABOUT (EXCLUSIONS):
• professor/instructor names, university/institution names, course numbers/titles
• emails, office hours, dates, schedules, room numbers, grading, website links
• credits/acknowledgements, page numbers, figure captions without substance

RULES:
• Every `front` MUST be a question and end with "?".
• NEVER use cloze markup {{c1::...}}.
• Focus ONLY on domain content: definitions, mechanisms, pathways, causes, functions, comparisons, equations, conditions, and key outcomes central to the topic.

OUTPUT FORMAT (STRICT JSON):
{
  "cards": [
    { "front": "...?", "back": "..." }
  ]
}
""".strip()


SYSTEM_PROMPT_CLOZE = """
You are an expert Anki CLOZE card writer for science/medicine PDFs.

TASK:
Generate ONLY valid Anki CLOZE cards.

ABSOLUTE REQUIREMENTS (NON-NEGOTIABLE):
• Each card MUST contain exactly ONE cloze deletion using DOUBLE braces:
  {{c1::hidden text}}
• Cards WITHOUT {{c1::...}} MUST NOT be included.
• DO NOT produce questions.
• DO NOT use "What", "Which", "Why", "How", "Who", or "When".
• Rewrite content into a declarative sentence BEFORE clozing.
• Hide a key concept or phrase (2–8 words), not an entire sentence.

STYLE RULES:
• `front` MUST be a single declarative fact.
• `back` should be empty or ≤ 15 words (optional clarification).
• Prefer concise, high-yield facts over explanations.

EXCLUSIONS:
• instructor/university/admin content
• dates, schedules, emails, credits, page numbers

STRICT OUTPUT FORMAT (JSON ONLY):
{
  "cards": [
    { "front": "Declarative sentence with {{c1::hidden phrase}}.", "back": "" }
  ]
}

If no suitable cloze facts exist, return:
{ "cards": [] }
""".strip()

def generate_color_table_revision(
    source_text: str,
    existing_entries: list,
    deck_hint: str,
    api_key: str,
    max_new_terms: int = 160,
):
    """
    Return a FULL, reorganized color table:
    - Reassigns groups
    - Creates new groups
    - Assigns one pastel color per group
    - Adds very specific domain terms
    """
    system_prompt = f"""
You maintain a color-coding dictionary for Anki study decks.

TASK:
- Return a FULL list of entries {{word, group, color}}.
- You MAY reorganize existing entries freely.
- You MAY create new groups and assign new colors.
- You MAY add very specific domain terms (receptor subtypes, ions, Greek letters).
- All words in the same group MUST share the same color.
- Use soft, readable pastel HEX colors suitable for dark mode.

RULES:
- Canonical singular words.
- Avoid duplicates (case-insensitive).
- Keep output concise and high-yield.
- Add up to {max_new_terms} new words.

OUTPUT (JSON ONLY):
{{ "entries": [{{"word":"","group":"","color":""}}] }}
""".strip()

    user_prompt = f"""
Deck: {deck_hint}

Existing table (you may reorganize freely):
{existing_entries}

Study text:
\"\"\"
{source_text}
\"\"\"
"""

    import requests, json
    resp = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
        },
        timeout=120,
    )
    resp.raise_for_status()

    try:
        raw = resp.json()["choices"][0]["message"]["content"]
        obj = json.loads(raw)
        return obj.get("entries", [])
    except Exception:
        return []

def generate_color_table_entries(
    source_text: str,
    existing_entries: list,
    deck_hint: str,
    api_key: str,
):
    system_prompt = """
You are helping maintain a color-coding dictionary for Anki study decks.

TASK:
- Analyze the provided study text.
- Propose NEW vocabulary terms that deserve consistent coloring.
- Group them semantically.
- Assign soft, readable HEX colors (pastel, dark-mode friendly).

PRIORITY RULES (VERY IMPORTANT):

1. STRONGLY PREFER SPECIFIC TERMINOLOGY.
   Add highly specific, domain-level terms such as:
   - receptor subtypes (e.g., α7 nAChR, KvA, KvD)
   - ion channels (e.g., AQP4, cyclic nucleotide–gated channels)
   - signaling molecules (e.g., cAMP, IP₃, Ca²⁺)
   - named systems, pathways, proteins, or cell types
   - anatomical or physiological structures with concrete names

2. DEPRIORITIZE OR SKIP GENERIC TERMS unless already present.
   Avoid adding vague or high-level words such as:
   - stimulus, process, system, activity, response, energy
   - general verbs or abstract nouns

3. IF A SPECIFIC TERM EXISTS, DO NOT ADD ITS GENERIC PARENT.
   Example:
   - Prefer “α7 nicotinic acetylcholine receptor”
   - Do NOT add “nicotinic receptor” or “receptor” unless missing and essential

4. Canonicalize terms:
   - Use standard scientific naming
   - Keep Greek letters (α, β, γ) when appropriate
   - Singular form unless plural is standard

5. Output ONLY new entries that are not already present
   (case-insensitive comparison).

FORMAT:
{
  "entries": [
    { "word": "...", "group": "...", "color": "#RRGGBB" }
  ]
}
""".strip()

    user_prompt = f"""
Deck: {deck_hint}

Existing entries:
{json.dumps(existing_entries, ensure_ascii=False)}

Study text:
\"\"\"
{source_text}
\"\"\"
"""


    # ===============================
    # DEBUG: log exact OpenAI payload
    # ===============================
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.2,
    }

    _dbg("=== AI COLOR TABLE REQUEST DEBUG ===")
    _dbg(f"OPENAI_API_URL={OPENAI_API_URL}")
    _dbg(f"model={payload['model']}")
    _dbg(f"temperature={payload['temperature']}")
    _dbg(f"messages_len={len(payload['messages'])}")

    for i, m in enumerate(payload["messages"]):
        _dbg(f"message[{i}].role={m.get('role')}")
        _dbg(f"message[{i}].content_type={type(m.get('content'))}")
        if isinstance(m.get("content"), str):
            _dbg(f"message[{i}].content_len={len(m['content'])}")
        else:
            _dbg(f"message[{i}].content_repr={repr(m.get('content'))}")

    _dbg("=== END AI COLOR TABLE REQUEST DEBUG ===")

    resp = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()

    try:
        data = json.loads(
            resp.json()["choices"][0]["message"]["content"]
        )
        return data.get("entries", [])
    except Exception:
        return []

def build_user_prompt_basic(text: str) -> str:
    return f"""
Create Anki **Basic** (Q→A) flashcards from the lecture text below.
Return ONLY content cards; **exclude** instructor/university/admin/schedule/contact/credits.

Lecture text:
\"\"\"
{text}
\"\"\"

Return JSON exactly as:
{{
  "cards": [
    {{ "front": "…?", "back": "…" }}
  ]
}}
""".strip()


def build_user_prompt_cloze(text: str) -> str:
    return f"""
Create Anki **Cloze** flashcards from the lecture text below.

STRICT RULES:
• Every card MUST contain exactly ONE {{c1::...}}.
• Cards WITHOUT cloze markup MUST NOT be included.
• Use declarative sentences only (no questions).

Lecture text:
\"\"\"
{text}
\"\"\"

Return JSON exactly as:
{{
  "cards": [
    {{ "front": "Declarative sentence with {{c1::hidden phrase}}.", "back": "" }}
  ]
}}
""".strip()



def generate_cards(lecture_text: str, api_key: str, mode: str) -> Dict:
    system_prompt = SYSTEM_PROMPT_BASIC if mode == "basic" else SYSTEM_PROMPT_CLOZE
    user_prompt   = build_user_prompt_basic(lecture_text) if mode == "basic" else build_user_prompt_cloze(lecture_text)

    response = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt}
            ],
            "temperature": TEMPERATURE,
            # Optional but helps: enforce JSON object output
            "response_format": {"type": "json_object"}
        },
        timeout=120
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return {"cards": []}
