
# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import json
import re
from dataclasses import dataclass
from typing import Dict, List, Iterable, Tuple

from aqt import mw, gui_hooks
from aqt.qt import (
    QAction,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QColor,
    QDialog,
    QGuiApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QFileDialog,
    QWidget,
    Qt,
)
from aqt.utils import showInfo

# -------------------------------------------------------------------
# Paths & constants
# -------------------------------------------------------------------
ADDON_DIR = os.path.dirname(__file__)
DATA_PATH = os.path.join(ADDON_DIR, "colorcoding_data.json")

# -------------------------------------------------------------------
# Add-on identity for config I/O
# -------------------------------------------------------------------
def _detect_addon_id() -> str:
    try:
        mod = __name__
        a_id = mw.addonManager.addonFromModule(mod)
        if a_id:
            return a_id
    except Exception:
        pass
    return os.path.basename(ADDON_DIR)

ADDON_ID = _detect_addon_id()

# -------------------------------------------------------------------
# Config helpers
# -------------------------------------------------------------------
def _read_cfg() -> dict:
    try:
        cfg = mw.addonManager.getConfig(ADDON_ID)
    except Exception:
        cfg = {}
    return cfg if isinstance(cfg, dict) else {}

def _write_cfg(cfg: dict) -> None:
    if not isinstance(cfg, dict):
        cfg = {}
    try:
        mw.addonManager.writeConfig(ADDON_ID, cfg)
    except Exception:
        pass

def _ensure_cfg_initialized() -> dict:
    cfg = _read_cfg()
    if "color_entries" not in cfg or not isinstance(cfg["color_entries"], list):
        cfg["color_entries"] = []
    if "bold_enabled" not in cfg:
        cfg["bold_enabled"] = True
    if "italic_enabled" not in cfg:
        cfg["italic_enabled"] = False
    if "bold_plurals_enabled" not in cfg:
        cfg["bold_plurals_enabled"] = True
    if "colorize_enabled" not in cfg:
        cfg["colorize_enabled"] = True
    if "whole_words" not in cfg:
        cfg["whole_words"] = True
    if "case_insensitive" not in cfg:
        cfg["case_insensitive"] = True
    _write_cfg(cfg)
    return cfg

# -------------------------------------------------------------------
# Color table storage
# -------------------------------------------------------------------
def _load_entries_from_json() -> List[dict]:
    try:
        if os.path.exists(DATA_PATH):
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []

def _save_entries_to_json(entries: List[dict]) -> None:
    try:
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_color_table() -> Dict[str, str]:
    """
    Build {word -> color} from config editor entries.
    Each entry: {"word": "...", "group": "...", "color": "#..."}
    """
    table: Dict[str, str] = {}
    cfg = _ensure_cfg_initialized()
    entries = cfg.get("color_entries", [])
    if isinstance(entries, list) and entries:
        for row in entries:
            if isinstance(row, dict):
                w = str(row.get("word", "")).strip()
                c = str(row.get("color", "")).strip()
                if w and c:
                    table[w] = c
        return table

    # Fallback: legacy JSON file next to the add-on
    for row in _load_entries_from_json():
        if isinstance(row, dict):
            w = str(row.get("word", "")).strip()
            c = str(row.get("color", "")).strip()
            if w and c:
                table[w] = c
    return table

def get_entries_for_editor() -> List[dict]:
    cfg = _ensure_cfg_initialized()
    entries = cfg.get("color_entries", [])
    if isinstance(entries, list) and entries:
        return entries
    return _load_entries_from_json()

def set_color_table_entries(entries: List[dict]) -> None:
    cfg = _ensure_cfg_initialized()
    cfg["color_entries"] = entries
    _write_cfg(cfg)
    _save_entries_to_json(entries)

# -------------------------------------------------------------------
# Utility: Color swatch rendering
# -------------------------------------------------------------------
def _qcolor_from_str(s: str) -> QColor:
    qc = QColor(s)
    return qc if qc.isValid() else QColor(Qt.GlobalColor.transparent)

def _luminance(qc: QColor) -> float:
    r, g, b = qc.redF(), qc.greenF(), qc.blueF()
    return 0.2126 * r + 0.7152 * g + 0.0722 * b

def _set_color_cell_visual(item: QTableWidgetItem) -> None:
    if item is None:
        return
    qc = _qcolor_from_str(item.text().strip())
    if not qc.isValid():
        item.setBackground(QColor(Qt.GlobalColor.transparent))
        item.setForeground(QColor(Qt.GlobalColor.black))
        return
    item.setBackground(qc)
    fg = QColor(Qt.GlobalColor.black if _luminance(qc) > 0.6 else Qt.GlobalColor.white)
    item.setForeground(fg)

# -------------------------------------------------------------------
# Color Table Editor Dialog
# -------------------------------------------------------------------
class ColorTableEditor(QDialog):
    COL_WORD = 0
    COL_COLOR = 1
    COL_GROUP = 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Color Table")
        self.resize(780, 540)
        self._suppress_item_changed = False

        main = QVBoxLayout(self)
        main.addWidget(QLabel("Define words/terms and their colors.\nColumns: Word, Color, Group."))

        self.table = QTableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["Word", "Color", "Group"])
        self.table.horizontalHeader().setStretchLastSection(True)
        main.addWidget(self.table)

        row1 = QHBoxLayout()
        self.btn_add = QPushButton("Add row")
        self.btn_remove = QPushButton("Remove selected")
        self.btn_pick = QPushButton("Pick color…")
        row1.addWidget(self.btn_add)
        row1.addWidget(self.btn_remove)
        row1.addWidget(self.btn_pick)
        row1.addStretch(1)
        main.addLayout(row1)

        row2 = QHBoxLayout()
        self.btn_import = QPushButton("Import JSON…")
        self.btn_export = QPushButton("Export JSON…")
        self.btn_paste = QPushButton("Paste JSON…")
        self.btn_append_paste = QPushButton("Append JSON…")  # NEW: non-destructive
        self.btn_copy = QPushButton("Copy JSON")
        row2.addWidget(self.btn_import)
        row2.addWidget(self.btn_export)
        row2.addSpacing(16)
        row2.addWidget(self.btn_paste)
        row2.addWidget(self.btn_append_paste)  # NEW
        row2.addWidget(self.btn_copy)
        row2.addStretch(1)
        main.addLayout(row2)

        okrow = QHBoxLayout()
        self.btn_cancel = QPushButton("Cancel")
        self.btn_save = QPushButton("Save")
        okrow.addStretch(1)
        okrow.addWidget(self.btn_cancel)
        okrow.addWidget(self.btn_save)
        main.addLayout(okrow)

        self.btn_add.clicked.connect(self._add_row)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_pick.clicked.connect(self._pick_color_for_selected)
        self.btn_import.clicked.connect(self._import_json)
        self.btn_export.clicked.connect(self._export_json)
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_paste.clicked.connect(self._paste_json_dialog)
        self.btn_append_paste.clicked.connect(self._append_json_dialog)  # NEW
        self.btn_copy.clicked.connect(self._copy_json_to_clipboard)
        self.table.itemChanged.connect(self._on_item_changed)

        self._load_entries(get_entries_for_editor())
        self._refresh_color_swatches()

    def _load_entries(self, entries: List[dict]):
        self._suppress_item_changed = True
        try:
            self.table.setRowCount(0)
            for row in entries:
                if not isinstance(row, dict):
                    continue
                word = str(row.get("word", "")).strip()
                color = str(row.get("color", "")).strip()
                group = str(row.get("group", "")).strip()
                if word and color:
                    self._append_row(word, color, group)
        finally:
            self._suppress_item_changed = False

    def _append_row(self, word="", color="", group=""):
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, self.COL_WORD, QTableWidgetItem(word))
        color_item = QTableWidgetItem(color)
        self.table.setItem(r, self.COL_COLOR, color_item)
        self.table.setItem(r, self.COL_GROUP, QTableWidgetItem(group))
        _set_color_cell_visual(color_item)

    def _remove_selected(self):
        rows = sorted({idx.row() for idx in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)

    def _pick_color_for_selected(self):
        idxs = self.table.selectedIndexes()
        r = (idxs[0].row() if idxs else -1)
        if r < 0:
            return
        qcolor = QColorDialog.getColor()
        if qcolor.isValid():
            item = self.table.item(r, self.COL_COLOR)
            if item is None:
                item = QTableWidgetItem()
                self.table.setItem(r, self.COL_COLOR, item)
            item.setText(qcolor.name())
            _set_color_cell_visual(item)

    def _add_row(self):
        self._append_row()

    def _collect_entries(self) -> List[dict]:
        entries = []
        for r in range(self.table.rowCount()):
            w = self.table.item(r, self.COL_WORD)
            c = self.table.item(r, self.COL_COLOR)
            g = self.table.item(r, self.COL_GROUP)
            word = (w.text() if w else "").strip()
            color = (c.text() if c else "").strip()
            group = (g.text() if g else "").strip()
            if word and color:
                entries.append({"word": word, "group": group, "color": color})
        return entries

    def _on_save(self):
        self.table.setFocus(Qt.FocusReason.OtherFocusReason)
        QApplication.processEvents()
        entries = self._collect_entries()
        set_color_table_entries(entries)
        self.accept()

    def _import_json(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import JSON", "", "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._load_entries(data)
                self._refresh_color_swatches()
        except Exception:
            pass

    def _export_json(self):
        entries = self._collect_entries()
        path, _ = QFileDialog.getSaveFileName(self, "Export JSON", "colorcoding_data.json", "JSON Files (*.json);;All Files (*)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _paste_json_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Paste JSON (replace entire table)")
        vbox = QVBoxLayout(dlg)
        vbox.addWidget(QLabel("Paste a JSON array of {word, group, color} objects:"))
        text = QPlainTextEdit(dlg)
        text.setMinimumHeight(200)
        vbox.addWidget(text)
        btns = QHBoxLayout()
        btn_cancel = QPushButton("Cancel", dlg)
        btn_load = QPushButton("Load", dlg)
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_load)
        vbox.addLayout(btns)

        def do_load():
            raw = text.toPlainText().strip()
            if not raw:
                return
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    self._load_entries(data)
                    self._refresh_color_swatches()
                    dlg.accept()
            except Exception as e:
                QMessageBox.critical(self, "Paste JSON – Error", f"{type(e).__name__}: {e}")

        btn_cancel.clicked.connect(dlg.reject)
        btn_load.clicked.connect(do_load)
        dlg.exec()

    # --- NEW: non-destructive append of pasted JSON entries ---
    def _append_entries(self, incoming: List[dict]) -> Tuple[int, int]:
        """
        Merge 'incoming' entries into the current table without replacing the whole table.
        Duplicate policy: if a 'word' already exists, SKIP it (keep existing).
        Returns (added_count, skipped_count).
        """
        existing_words = set()
        for r in range(self.table.rowCount()):
            w_item = self.table.item(r, self.COL_WORD)
            word = (w_item.text() if w_item else "").strip()
            if word:
                existing_words.add(word)

        added = 0
        skipped = 0
        for row in incoming:
            if not isinstance(row, dict):
                continue
            word = str(row.get("word", "")).strip()
            color = str(row.get("color", "")).strip()
            group = str(row.get("group", "")).strip()
            if not (word and color):
                continue

            if word in existing_words:
                skipped += 1
                # OVERWRITE policy (optional):
                # If you prefer to update existing entries instead of skipping,
                # replace this block with code that finds the row and updates color/group.
                continue

            self._append_row(word, color, group)
            existing_words.add(word)
            added += 1

        return added, skipped

    def _append_json_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Append JSON (merge without replacing)")
        vbox = QVBoxLayout(dlg)
        vbox.addWidget(QLabel(
            "Paste a JSON array of {word, group, color} objects.\n"
            "Existing words are kept; only new words are added."
        ))
        text = QPlainTextEdit(dlg)
        text.setMinimumHeight(200)
        vbox.addWidget(text)

        btns = QHBoxLayout()
        btn_cancel = QPushButton("Cancel", dlg)
        btn_append = QPushButton("Append", dlg)
        btns.addStretch(1)
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_append)
        vbox.addLayout(btns)

        def do_append():
            raw = text.toPlainText().strip()
            if not raw:
                return
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    added, skipped = self._append_entries(data)
                    self._refresh_color_swatches()
                    QMessageBox.information(
                        self,
                        "Append JSON",
                        f"Appended {added} new entr{'y' if added == 1 else 'ies'}.\n"
                        f"Skipped {skipped} duplicate{'s' if skipped != 1 else ''}."
                    )
                    dlg.accept()
            except Exception as e:
                QMessageBox.critical(self, "Append JSON – Error", f"{type(e).__name__}: {e}")

        btn_cancel.clicked.connect(dlg.reject)
        btn_append.clicked.connect(do_append)
        dlg.exec()

    def _copy_json_to_clipboard(self):
        entries = self._collect_entries()
        payload = json.dumps(entries, ensure_ascii=False, indent=2)
        QGuiApplication.clipboard().setText(payload)

    def _refresh_color_swatches(self):
        self._suppress_item_changed = True
        try:
            for r in range(self.table.rowCount()):
                item = self.table.item(r, self.COL_COLOR)
                if item:
                    _set_color_cell_visual(item)
        finally:
            self._suppress_item_changed = False

    def _on_item_changed(self, item: QTableWidgetItem):
        if self._suppress_item_changed:
            return
        if item.column() == self.COL_COLOR:
            _set_color_cell_visual(item)

# -------------------------------------------------------------------
# Deck listing helper
# -------------------------------------------------------------------
def deck_names_with_children_flag() -> Dict[str, bool]:
    """
    Return {deck_name: True} for all decks. Compatible across Anki versions.
    """
    try:
        decks = mw.col.decks.all_names_and_ids()  # modern Anki
        if isinstance(decks, list) and decks and isinstance(decks[0], tuple):
            return {name: True for (name, _id) in decks}
    except Exception:
        pass
    try:
        names = [d["name"] for d in mw.col.decks.all()]  # older fallback
        return {n: True for n in names}
    except Exception:
        return {}

# -------------------------------------------------------------------
# Deck picker with Bold/Italic/Plural/Colorize/Whole/Case options
# -------------------------------------------------------------------
class DeckPickerDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Apply Color Coding to Selected Decks")
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select one or more decks:"))

        self.deck_list = QListWidget(self)
        self.deck_list.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        for d in sorted(deck_names_with_children_flag().keys()):
            self.deck_list.addItem(QListWidgetItem(d))
        layout.addWidget(self.deck_list)

        # Options
        self.include_children_cb = QCheckBox("Include subdecks", self)
        self.include_children_cb.setChecked(True)
        self.skip_cloze_cb = QCheckBox("Skip Cloze models", self)
        self.skip_cloze_cb.setChecked(False)

        cfg = _ensure_cfg_initialized()
        self.whole_words_cb = QCheckBox("Whole words only", self)
        self.whole_words_cb.setChecked(cfg.get("whole_words", True))
        self.case_insensitive_cb = QCheckBox("Case insensitive", self)
        self.case_insensitive_cb.setChecked(cfg.get("case_insensitive", True))

        # Style toggles
        self.bold_cb = QCheckBox("Bold words", self)
        self.bold_cb.setChecked(cfg.get("bold_enabled", True))
        self.italic_cb = QCheckBox("Italic words", self)
        self.italic_cb.setChecked(cfg.get("italic_enabled", False))
        self.bold_plurals_cb = QCheckBox('Match plural forms (last token only)', self)
        self.bold_plurals_cb.setChecked(cfg.get("bold_plurals_enabled", True))
        self.colorize_cb = QCheckBox('Colorize words (turn off to decolor)', self)
        self.colorize_cb.setChecked(cfg.get("colorize_enabled", True))

        for cb in [
            self.include_children_cb,
            self.skip_cloze_cb,
            self.whole_words_cb,
            self.case_insensitive_cb,
            self.bold_cb,
            self.italic_cb,
            self.bold_plurals_cb,
            self.colorize_cb,
        ]:
            layout.addWidget(cb)

        # Buttons
        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.cancel_btn = QPushButton("Cancel")
        self.run_btn.clicked.connect(self.accept)
        self.cancel_btn.clicked.connect(self.reject)
        btn_row.addStretch(1)
        self.cancel_btn.setDefault(False)
        self.run_btn.setDefault(True)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.run_btn)
        layout.addLayout(btn_row)

    def selected_decks(self) -> List[str]:
        return [i.text() for i in self.deck_list.selectedItems()]

    def include_children(self) -> bool:
        return self.include_children_cb.isChecked()

    def skip_cloze(self) -> bool:
        return self.skip_cloze_cb.isChecked()

    def whole_words(self) -> bool:
        return self.whole_words_cb.isChecked()

    def case_insensitive(self) -> bool:
        return self.case_insensitive_cb.isChecked()

    def bold_enabled(self) -> bool:
        return self.bold_cb.isChecked()

    def italic_enabled(self) -> bool:
        return self.italic_cb.isChecked()

    def bold_plurals_enabled(self) -> bool:
        return self.bold_plurals_cb.isChecked()

    def colorize_enabled(self) -> bool:
        return self.colorize_cb.isChecked()

# -------------------------------------------------------------------
# Core coloring helpers (PERMANENT multi-word fix + longest-first priority)
# -------------------------------------------------------------------
@dataclass
class ColoringOptions:
    whole_words: bool = True
    case_insensitive: bool = True
    bold: bool = True
    italic: bool = False
    bold_plurals: bool = True  # "Match plural forms" toggle
    colorize: bool = True

# --- Helpers for multi-word & plural support (no term lists) ---
_CAMEL_TOKEN_RE = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")

# Flexible separators allowed between tokens on cards:
# space, &nbsp;, hyphen, slash, en dash, em dash, or a simple inline HTML tag
_SEP_RE = r"(?:\s|&nbsp;|[-/]|–|—|<[^>]+?>)+"

def _tokenize_term(term: str) -> List[str]:
    """
    Split a color-table key into tokens:
      - If it contains spaces/underscores/hyphens -> split on those
      - Else -> split CamelCase into tokens
    """
    if re.search(r"[\s_\-]", term):
        raw = re.split(r"[\s_\-]+", term)
        return [t for t in raw if t]
    return _CAMEL_TOKEN_RE.findall(term)

def _plural_last_token_pattern(base: str, case_insensitive: bool) -> str:
    """
    Return a regex that matches a token's singular and common plural variants by RULES (no word lists).
    Rules applied in order (classical + English):
      - us -> i        (nucleus->nuclei)
      - um -> a        (septum->septa)
      - on -> a        (ganglion->ganglia)
      - is -> es       (anastomosis->anastomoses)
      - ex/ix -> ices  (cortex/index->cortices/indices)
      - ma -> mata     (stoma->stomata)
      - men -> mina    (foramen->foramina)
      - x -> ces       (thorax->thoraces)
      - [^aeiou]y -> ies  (artery->arteries)
      - (s|x|z|ch|sh) -> +es
      - default -> +s
    """
    b = base.lower() if case_insensitive else base
    cand = [base]

    if re.search(r"us$", b):
        cand.append(base[:-2] + "i")
    elif re.search(r"um$", b):
        cand.append(base[:-2] + "a")
    elif re.search(r"on$", b):
        cand.append(base[:-2] + "a")
    elif re.search(r"(?:ex|ix)$", b):
        cand.append(base[:-2] + "ices")
    elif re.search(r"is$", b):
        cand.append(base[:-2] + "es")
    elif re.search(r"ma$", b):
        cand.append(base[:-2] + "mata")
    elif re.search(r"men$", b):
        cand.append(base[:-3] + "mina")
    elif re.search(r"x$", b):
        cand.append(base[:-1] + "ces")
    elif re.search(r"[^aeiou]y$", b):
        cand.append(base[:-1] + "ies")
    elif re.search(r"(s|x|z|ch|sh)$", b):
        cand.append(base + "es")
    else:
        cand.append(base + "s")

    alts = "|".join(re.escape(c) for c in cand)
    return f"(?:{alts})"

def build_combined_regex(color_table: Dict[str, str], opts: ColoringOptions) -> Tuple[re.Pattern, Dict[str, str]]:
    """
    Build one combined regex with named groups (k0, k1, ...) for each table key, supporting:
      - multi-word (spaced) matching from CamelCase/snake_case/hyphenated/space keys,
      - flexible separators on cards (spaces, &nbsp;, hyphen, slash, en/em dash, simple HTML tags),
      - pluralization of the LAST token when enabled,
      - whole-words behavior per token (when whole_words=True),
      - LONGEST-FIRST PRIORITY so specific phrases beat generic tokens.
    Returns: (compiled_regex, groupname_to_color)
    """
    alts: List[str] = []
    group_to_color: Dict[str, str] = {}

    # --- Pre-tokenize and sort: longer/specific first ---
    tmp: List[Tuple[List[str], str, str]] = []  # (tokens, key, color)
    for key, color in color_table.items():
        tokens = _tokenize_term(key)
        if tokens:
            tmp.append((tokens, key, color))

    # Sort by: (1) token count desc, (2) total chars desc
    tmp.sort(key=lambda t: (len(t[0]), sum(len(tok) for tok in t[0])), reverse=True)
    # -----------------------------------------------------

    i = 0
    for tokens, key, color in tmp:
        def tok_piece(tok: str, is_last: bool) -> str:
            if is_last and opts.bold_plurals:
                last_core = _plural_last_token_pattern(tok, opts.case_insensitive)
            else:
                last_core = re.escape(tok)

            if opts.whole_words:
                if is_last:
                    return rf"\b{last_core}\b"
                else:
                    return rf"\b{re.escape(tok)}\b"
            else:
                if is_last and not opts.bold_plurals and len(tokens) == 1:
                    # avoid matching stem just before trivial plural "s"
                    return rf"{re.escape(tok)}(?!s)"
                if is_last:
                    return last_core
                return re.escape(tok)

        if len(tokens) == 1:
            entry_pat = tok_piece(tokens[0], True)
        else:
            parts = [tok_piece(t, False) for t in tokens[:-1]]
            parts.append(tok_piece(tokens[-1], True))
            entry_pat = _SEP_RE.join(parts)

        gname = f"k{i}"
        alts.append(f"(?P<{gname}>{entry_pat})")
        group_to_color[gname] = color
        i += 1

    pattern = "|".join(alts) if alts else r"(?!x)x"
    flags = re.IGNORECASE if opts.case_insensitive else 0
    return re.compile(pattern, flags), group_to_color

def apply_color_coding_to_html(
    html: str,
    regex: re.Pattern,
    group_to_color: Dict[str, str],
    opts: ColoringOptions,
) -> Tuple[str, int]:
    if not html or not regex.pattern:
        return html, 0

    # Normalize first: strip old wrappers so toggles reflect current state
    html = re.sub(
        r'<span class="cc-color"[^>]*>(.*?)</span>',
        r'\1',
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )

    parts = re.split(r"(<[^>]+>)", html)  # split into text/tag chunks
    changed = False
    total = 0

    def repl(m: re.Match) -> str:
        nonlocal total
        gname = m.lastgroup
        if not gname:
            return m.group(0)
        color = group_to_color.get(gname)
        if not color:
            return m.group(0)

        style_bits = []
        if opts.colorize:
            style_bits.append(f"color:{color};")
        if opts.bold:
            style_bits.append("font-weight:bold;")
        if opts.italic:
            style_bits.append("font-style:italic;")

        if not style_bits:
            return m.group(0)

        total += 1
        style = " ".join(style_bits)
        return f'<span class="cc-color" style="{style}">{m.group(0)}</span>'

    for i, chunk in enumerate(parts):
        if i % 2 == 0 and chunk and "cc-color" not in chunk:
            new_chunk, n = regex.subn(repl, chunk)
            if n:
                parts[i] = new_chunk
                changed = True

    if not changed:
        return html, 0
    return "".join(parts), total

# -------------------------------------------------------------------
# Other helpers
# -------------------------------------------------------------------
def note_is_cloze(note) -> bool:
    try:
        mt = note.note_type()
        return (mt.get("type") == 1) or ("Cloze" in (mt.get("name") or ""))
    except Exception:
        return False

def quote_deck_for_search(deck_name: str) -> str:
    safe = deck_name.replace('"', '\\"')
    return f'deck:"{safe}"'

# -------------------------------------------------------------------
# Batch processor
# -------------------------------------------------------------------
def color_notes_in_decks(
    deck_names: Iterable[str],
    include_children: bool,
    skip_cloze: bool,
    opts: ColoringOptions,
) -> Tuple[int, int, int]:
    color_table = get_color_table()
    if not color_table:
        raise RuntimeError("Color table is empty. Configure your color mappings first.")
    regex, group_to_color = build_combined_regex(color_table, opts)

    notes_seen = 0
    notes_modified = 0
    total_replacements = 0

    # Build search query
    queries = []
    for deck in deck_names:
        if include_children:
            safe = deck.replace('"', '\\"')
            queries.append(f'(deck:"{safe}" OR deck:"{safe}::*")')
        else:
            queries.append(quote_deck_for_search(deck))
    if not queries:
        return 0, 0, 0

    search = " OR ".join(queries)

    mw.checkpoint("Color Coding Global")
    mw.progress.start(label="Color Coding: scanning notes…", immediate=True, min=0, max=0)
    try:
        nids = mw.col.find_notes(search)

        for idx, nid in enumerate(nids):
            if mw.progress.want_cancel():
                break

            note = mw.col.get_note(nid)
            notes_seen += 1

            if skip_cloze and note_is_cloze(note):
                continue

            modified = False
            replacements_for_note = 0

            for fname in note.keys():
                original = note[fname]
                new_val, num = apply_color_coding_to_html(original, regex, group_to_color, opts)
                # Save even if only normalization changed
                if new_val != original:
                    note[fname] = new_val
                    modified = True
                    replacements_for_note += num

            if modified:
                notes_modified += 1
                total_replacements += replacements_for_note
                note.flush()

            if idx % 200 == 0:
                mw.progress.update(label=f"Processing notes… ({idx+1}/{len(nids)})")

        mw.reset()  # refresh UI

    finally:
        mw.progress.finish()

    return notes_seen, notes_modified, total_replacements

# -------------------------------------------------------------------
# Menu actions
# -------------------------------------------------------------------
def _exec_dialog(dlg) -> int:
    try:
        return dlg.exec()
    except AttributeError:
        return dlg.exec_()

def _accepted_code() -> int:
    try:
        return int(QDialog.DialogCode.Accepted)
    except AttributeError:
        return int(QDialog.Accepted)

def on_apply_to_selected_decks():
    if mw is None or mw.col is None:
        QMessageBox.warning(mw, "Color Coding Global", "Collection is not open.")
        return

    dlg = DeckPickerDialog(mw)
    result = _exec_dialog(dlg)
    if int(result) != _accepted_code():
        return

    decks = dlg.selected_decks()
    if not decks:
        return

    include_children = dlg.include_children()
    skip_cloze = dlg.skip_cloze()
    opts = ColoringOptions(
        whole_words=dlg.whole_words(),
        case_insensitive=dlg.case_insensitive(),
        bold=dlg.bold_enabled(),
        italic=dlg.italic_enabled(),
        bold_plurals=dlg.bold_plurals_enabled(),
        colorize=dlg.colorize_enabled(),
    )

    # Remember preferences for next time
    cfg = _ensure_cfg_initialized()
    cfg["bold_enabled"] = dlg.bold_enabled()
    cfg["italic_enabled"] = dlg.italic_enabled()
    cfg["bold_plurals_enabled"] = dlg.bold_plurals_enabled()
    cfg["colorize_enabled"] = dlg.colorize_enabled()
    cfg["whole_words"] = dlg.whole_words()
    cfg["case_insensitive"] = dlg.case_insensitive()
    _write_cfg(cfg)

    try:
        notes_seen, notes_modified, total_replacements = color_notes_in_decks(
            deck_names=decks,
            include_children=include_children,
            skip_cloze=skip_cloze,
            opts=opts,
        )
    except Exception as e:
        QMessageBox.critical(mw, "Color Coding – Error", f"{type(e).__name__}: {e}")
        return

    showInfo(
        f"Color coding complete.\n\n"
        f"Decks: {', '.join(decks)}\n"
        f"Include subdecks: {'Yes' if include_children else 'No'}\n"
        f"Notes scanned: {notes_seen}\n"
        f"Notes modified: {notes_modified}\n"
        f"Total replacements: {total_replacements}"
    )

def on_edit_color_table():
    try:
        dlg = ColorTableEditor(mw)
        _exec_dialog(dlg)
    except Exception as e:
        QMessageBox.critical(mw, "Edit Color Table – Error", f"{type(e).__name__}: {e}")

def add_menu_action():
    menu = getattr(mw.form, "menuTools", None)
    if not menu:
        return
    submenu = menu.addMenu("Color Coding Global (Deck Picker)")
    action_run = QAction("Apply to Selected Decks…", mw)
    action_run.triggered.connect(on_apply_to_selected_decks)
    submenu.addAction(action_run)
    action_edit = QAction("Edit Color Table…", mw)
    action_edit.triggered.connect(on_edit_color_table)
    submenu.addAction(action_edit)

def open_coloration_settings_dialog():
    """
    Open a simple settings dialog (no deck selection) for coloration behavior.
    Saves to the colorizer config on OK; discards on Cancel.
    """
    from aqt import mw
    from aqt.qt import (
        QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton
    )
    from aqt.utils import showWarning

    try:
        # Use the same read/write helpers your add-on already has
        from . import _read_cfg as _cc_read_cfg
        from . import _write_cfg as _cc_write_cfg

        cfg = _cc_read_cfg() or {}

        dlg = QDialog(mw)
        dlg.setWindowTitle("Coloration Settings")
        v = QVBoxLayout(dlg)

        # Build checkboxes bound to your config keys
        chk_whole = QCheckBox("Whole words only")
        chk_casei = QCheckBox("Case insensitive")
        chk_bold = QCheckBox("Bold")
        chk_italic = QCheckBox("Italic")
        chk_bold_pl = QCheckBox("Bold plurals")
        chk_colorize = QCheckBox("Enable colorize")
        chk_skip_cloze = QCheckBox("Skip cloze notes")

        # Set current values from config (matching your keys)
        chk_whole.setChecked(bool(cfg.get("whole_words", True)))
        chk_casei.setChecked(bool(cfg.get("case_insensitive", True)))
        chk_bold.setChecked(bool(cfg.get("bold_enabled", True)))
        chk_italic.setChecked(bool(cfg.get("italic_enabled", False)))
        chk_bold_pl.setChecked(bool(cfg.get("bold_plurals_enabled", True)))
        chk_colorize.setChecked(bool(cfg.get("colorize_enabled", True)))
        # This key may or may not exist in your build; default to False
        chk_skip_cloze.setChecked(bool(cfg.get("skip_cloze", False)))

        v.addWidget(QLabel("Behavior options for automatic coloration:"))
        for w in (
            chk_whole, chk_casei, chk_bold, chk_italic,
            chk_bold_pl, chk_colorize, chk_skip_cloze
        ):
            v.addWidget(w)

        # Buttons row
        row = QHBoxLayout()
        btn_cancel = QPushButton("Cancel")
        btn_ok = QPushButton("OK")
        row.addStretch(1)
        row.addWidget(btn_cancel)
        row.addWidget(btn_ok)
        v.addLayout(row)

        def _save_and_close():
            # Persist updates to the colorizer config
            cfg["whole_words"] = chk_whole.isChecked()
            cfg["case_insensitive"] = chk_casei.isChecked()
            cfg["bold_enabled"] = chk_bold.isChecked()
            cfg["italic_enabled"] = chk_italic.isChecked()
            cfg["bold_plurals_enabled"] = chk_bold_pl.isChecked()
            cfg["colorize_enabled"] = chk_colorize.isChecked()
            cfg["skip_cloze"] = chk_skip_cloze.isChecked()
            _cc_write_cfg(cfg)
            dlg.accept()

        btn_ok.clicked.connect(_save_and_close)
        btn_cancel.clicked.connect(dlg.reject)

        dlg.exec()
    except Exception as e:
        showWarning(f"Could not open coloration settings: {e}")

# -------------------------------------------------------------------
# Initialize AFTER main window is ready
# -------------------------------------------------------------------
def _on_main_window_ready():
    _ensure_cfg_initialized()
    add_menu_action()

gui_hooks.main_window_did_init.append(_on_main_window_ready)





# ======================================================
# Programmatic API — uses same options as Deck Picker
# ======================================================
def apply_to_deck_ids(deck_ids, include_children=False, skip_cloze=None):
    """
    Apply color coding to the given deck(s).
    If skip_cloze is None, read it from the colorizer's saved settings
    (the same settings you edit in 'Coloration settings…').
    """
    from aqt import mw
    from aqt.utils import showWarning

    # Resolve deck names
    try:
        deck_names = [mw.col.decks.name(did) for did in deck_ids]
    except Exception as e:
        showWarning(f"Color coding failed (deck lookup): {e}")
        return

    # Safety guard (avoid huge auto runs)
    MAX_NOTES_AUTO = 1500
    try:
        if len(deck_names) == 1:
            count = len(mw.col.find_notes(f"deck:{deck_names[0]}"))
            if count > MAX_NOTES_AUTO:
                showWarning(
                    f"Auto-color skipped: deck has {count} notes.\n"
                    f"Use Tools → Color Coding Global instead."
                )
                return
    except Exception:
        pass

    # Build the same options object the UI uses
    try:
        from . import ColoringOptions, _read_cfg, color_notes_in_decks
        cfg = _read_cfg()

        # If caller didn't specify skip_cloze, read from saved settings
        if skip_cloze is None:
            # Most builds store this exact key; if not found, default False so cloze are colored.
            skip_cloze = bool(cfg.get("skip_cloze", False))

        options = ColoringOptions(
            whole_words=cfg.get("whole_words", True),
            case_insensitive=cfg.get("case_insensitive", True),
            bold=cfg.get("bold_enabled", True),
            italic=cfg.get("italic_enabled", False),
            bold_plurals=cfg.get("bold_plurals_enabled", True),
            colorize=cfg.get("colorize_enabled", True),
        )
    except Exception as e:
        showWarning(f"Color coding failed (options): {e}")
        return

    # Exact call signature used by your Deck Picker
    try:
        color_notes_in_decks(
            deck_names,
            include_children,
            skip_cloze,
            options,
        )
    except Exception as e:
        showWarning(f"Color coding failed: {e}")