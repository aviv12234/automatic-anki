
# openai_cards.py

import json
import requests
from typing import Dict

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL   = "gpt-4o-mini"
TEMPERATURE    = 0.2


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
