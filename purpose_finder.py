# purpose_finder.py
from __future__ import annotations
import html
import json
import time
from typing import Optional
import re

from aqt import mw, gui_hooks
from aqt.qt import (
    QAction, QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, Qt, QTextEdit
)
from aqt.utils import showWarning, tooltip

import requests


# ---------------------------------------------------------
# Logger
# ---------------------------------------------------------
def _dbg(msg: str) -> None:
    try:
        path = mw.pm.profileFolder() + "/pdf2cards_debug.log"
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [purpose] {msg}\n")
    except Exception:
        pass


# ---------------------------------------------------------
# Appearance
# ---------------------------------------------------------
PURPOSE_FONT_CSS = (
    'font-family: Verdana, sans-serif !important; '
    'font-size: 0.9em !important; '
)


# ---------------------------------------------------------
# Markdown + HTML + Emoji formatter
# ---------------------------------------------------------
def _format_ai_text(text: str) -> str:
    """Convert AI output into HTML with bold/italics/emoji enlargement."""

    if not text:
        return ""

    # Markdown bold → HTML bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)

    # Markdown italic → HTML italic
    text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)

    # Newlines → <br>
    text = text.replace("\n", "<br>")

    """
    # Emoji enlargement
    EMOJI_CSS = (
        'font-family: "Segoe UI Emoji", "Segoe UI Symbol", "Apple Color Emoji", "Noto Color Emoji", sans-serif; '
        'font-size: 1.5em; '
        'line-height: 1; '
        'vertical-align: -2px;'
    )
    
    emoji_pattern = re.compile(
        r'['
        r'\U0001F300-\U0001FAFF'   # modern emoji range
        r'\U0001F000-\U0001F9FF'   # older emoji range
        r']',
        flags=re.UNICODE
    )

    def wrap_emoji(match):
        e = match.group(0)
        return f'<span style="{EMOJI_CSS}">{e}</span>'

    text = emoji_pattern.sub(wrap_emoji, text)

    
    """

    return text


# ---------------------------------------------------------
# OpenAI defaults
# ---------------------------------------------------------
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_MODEL   = "gpt-4o-mini"
TEMPERATURE    = 0.1


# ---------------------------------------------------------
# Utilities
# ---------------------------------------------------------
def _note_is_cloze(note) -> bool:
    try:
        mt = note.note_type()
        return (mt.get("type") == 1) or ("Cloze" in (mt.get("name") or ""))
    except Exception:
        return False


def _append_to_back(note, html_block: str) -> None:
    """Append formatted HTML to a suitable back field."""

    if _note_is_cloze(note):
        preferred = ["Back Extra", "Extra", "Back", "Text"]
    else:
        preferred = ["Back", "Answer", "Extra"]

    field = None
    for f in preferred:
        if f in note:
            field = f
            break

    if not field:
        flds = [fld["name"] for fld in note.note_type().get("flds", [])]
        field = flds[1] if len(flds) >= 2 else flds[-1]

    current = note[field] or ""
    sep = "" if current.strip() == "" else "<br><br>"
    note[field] = current + sep + html_block
    note.flush()


def _current_card_and_note():
    try:
        card = mw.reviewer.card
        return card, card.note()
    except:
        return None, None


def _get_selection(webview) -> str:
    try:
        return (webview.page().selectedText() or "").strip()
    except:
        return ""


# ---------------------------------------------------------
# OpenAI call
# ---------------------------------------------------------
def _ask_purpose(selected_text: str, api_key: str) -> str:
    system_prompt = (
        "Provide a concise explanation (1–3 sentences) describing "
        "the purpose or function of the given term or phrase. "
        "Use formatting such **bold** when appropriate to mark imporant terms(can be used liberally)." #, or emojis when appropriate."
    )

    user_prompt = f"What is the purpose of: {selected_text}?"

    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt}
        ],
        "temperature": TEMPERATURE
    }

    resp = requests.post(
        OPENAI_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


# ---------------------------------------------------------
# Dialog window (HTML-enabled)
# ---------------------------------------------------------
class PurposeDialog(QDialog):
    def __init__(self, term: str, answer: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Purpose — {term[:80]}")
        self.setModal(True)
        self.setMinimumWidth(520)

        v = QVBoxLayout(self)

        header = QLabel(f"<b>Purpose of:</b> {html.escape(term)}")
        header.setTextFormat(Qt.TextFormat.RichText)
        v.addWidget(header)

        self.box = QTextEdit(self)
        self.box.setReadOnly(True)
        self.box.setHtml(_format_ai_text(answer))
        v.addWidget(self.box)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.btn_add = QPushButton("Add to Back")
        self.btn_close = QPushButton("Close")
        buttons.addWidget(self.btn_close)
        buttons.addWidget(self.btn_add)
        v.addLayout(buttons)

        self.btn_close.clicked.connect(self.reject)
        self.btn_add.clicked.connect(self.accept)

    def result(self) -> str:
        return self.box.toHtml()


# ---------------------------------------------------------
# Card injection block
# ---------------------------------------------------------
def _html_block(term: str, explanation: str) -> str:
    formatted = _format_ai_text(explanation)
    return (
        f'<div class="ai-purpose" style="{PURPOSE_FONT_CSS}">'
        f'{formatted}'
        f'</div>'
    )


# ---------------------------------------------------------
# Core execution
# ---------------------------------------------------------
def _ensure_api_key() -> Optional[str]:
    try:
        from .main import get_api_key
        return get_api_key()
    except:
        showWarning("OpenAI API key missing.")
        return None


def _run_find_purpose(selected_text: str):
    if not selected_text:
        tooltip("No text selected.")
        return

    api_key = _ensure_api_key()
    if not api_key:
        return

    mw.progress.start(label="Asking AI...", immediate=True)

    def bg():
        try:
            return _ask_purpose(selected_text, api_key)
        except Exception as e:
            return e

    def done(future):
        try:
            mw.progress.finish()
        except:
            pass

        try:
            result = future.result()
        except Exception as e:
            showWarning(f"AI error: {e}")
            return

        if isinstance(result, Exception):
            showWarning(f"AI error: {result}")
            return

        dlg = PurposeDialog(selected_text, result, mw)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            card, note = _current_card_and_note()
            if not note:
                showWarning("No active note.")
                return

            block = _html_block(selected_text, dlg.result())
            _append_to_back(note, block)
            tooltip("Added to back.")

            # Preserve answer side
            prev = getattr(mw.reviewer, "state", None)
            try:
                if hasattr(mw.reviewer, "_redraw_current_card"):
                    mw.reviewer._redraw_current_card()
                else:
                    mw.reset()
                if prev == "answer":
                    mw.reviewer._showAnswer()
            except:
                mw.reset()

    mw.taskman.run_in_background(bg, on_done=lambda fut: done(fut))


# ---------------------------------------------------------
# Context menu hook
# ---------------------------------------------------------
def _on_context_menu(webview, menu):
    if mw.state != "review":
        return

    sel = _get_selection(webview)
    if not sel:
        return

    act = QAction("Find purpose (AI)", mw)
    act.triggered.connect(lambda: _run_find_purpose(sel))
    menu.addAction(act)


def register_purpose_context_item():
    gui_hooks.webview_will_show_context_menu.append(_on_context_menu)
    _dbg("Find-purpose context menu registered.")