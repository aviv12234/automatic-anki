
# openai_cards.py

import json
import requests
from typing import Dict

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL   = "gpt-4o-mini"
TEMPERATURE    = 0.2


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

RULES:
- Do NOT repeat existing words.
- Prefer canonical singular forms.
- Group names should be concise and consistent.
- Output JSON only.

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

    resp = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": OPENAI_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
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
