
# Automatic Anki – PDF → Basic & Cloze (with slide images)

Generate **Anki** cards directly from a **PDF**:
- **Basic** (Q→A) and/or **Cloze** ({{c1::…}}) cards
- One **image per slide/page** automatically attached to the card back
- **No cloze rewriting** or regex gymnastics—LLM decides, the add‑on routes

This project intentionally keeps `main.py` **boring** and **predictable**:
- **AI (openai_cards.py)** decides card content & type
- **main.py** only **routes** cards to the right notetype and deck
- **Anki templates/CSS** decide how cards look

---

## Contents

- Installation
- Quick Start
- Configuration
- How It Works (Architecture)
- Note Types & Templates
- Logs & Debugging
- Troubleshooting
- Design Principles
- Known Limitations

---

## Installation

1. Close Anki.
2. Copy this add‑on folder (e.g. `automatic-anki/`) to:
   - **Windows**: `%APPDATA%\Anki2\addons21\`
   - **macOS**: `~/Library/Application Support/Anki2/addons21/`
   - **Linux**: `~/.local/share/Anki2/addons21/`
3. Start Anki. You should see a new menu item:
   - **Tools → Generate Anki cards from PDF**
   - **Tools → Check PDF Renderer (automatic-anki)** (optional diagnostics)

> The add‑on supports Anki 2.1.x (Qt6) and tries **PyMuPDF** first for PDF rendering; it can fall back to Qt PDF or embedded images when available.

---

## Quick Start

1. **Set API key** (first run)
   - When prompted, paste your **OpenAI API key**. It’s saved in add‑on config.
2. **Run generation**
   - **Tools → Generate Anki cards from PDF**
   - Pick your PDF (lecture slides / notes).
3. **Choose options**
   - **Card types**: check **Basic** and/or **Cloze**.
   - **Cards per slide**:
     - **AI (all)** – keep all returned cards
     - **Range (min–max)** – random slice per page (Cloze kept first)
4. **Result**
   - A deck named after the PDF filename is created.
   - Cards are inserted with the slide image on the back.

---

## Configuration

The add‑on stores minimal settings in Anki’s add‑on config (automatic):
- `openai_api_key`: your key
- `types_basic`: `true/false`
- `types_cloze`: `true/false`
- `per_slide_mode`: `"ai"` or `"range"`
- `per_slide_min` / `per_slide_max`: integers (1–50)

You can change these from **Tools → Generate Anki cards from PDF** (Options dialog), or via Anki’s **Add‑ons → Config** UI.

---

## How It Works (Architecture)

**One responsibility per layer**:

## How It Works (Architecture)

PDF
 └─▶ main.py
      ├─ calls pdf_parser.extract_text_from_pdf() to get per‑page text
      ├─ calls openai_cards.py for each page:
      │     • BASIC → question/answer cards
      │     • CLOZE → declarative cards with {{c1::…}}
      ├─ routes cards by presence of {{c}}
      │     • {{c}} present → Cloze + Slide
      │     • otherwise     → Basic + Slide
      ├─ attaches slide image to each note
      └─ force‑moves cards to the PDF‑named deck

Anki Templates & CSS
 └─ control layout, styling, cloze color, and images


# Automatic Anki – PDF → Basic & Cloze (with slide images)

Generate **Anki** cards directly from a **PDF**:
- **Basic** (Q→A) and/or **Cloze** ({{c1::…}}) cards
- One **image per slide/page** automatically attached to the card back
- **No cloze rewriting** or regex gymnastics—LLM decides, the add‑on routes

This project intentionally keeps `main.py` **boring** and **predictable**:
- **AI (openai_cards.py)** decides card content & type
- **main.py** only **routes** cards to the right notetype and deck
- **Anki templates/CSS** decide how cards look

---

## Contents

- Installation
- Quick Start
- Configuration
- How It Works (Architecture)
- Note Types & Templates
- Logs & Debugging
- Troubleshooting
- Design Principles
- Known Limitations

---

## Installation

1. Close Anki.
2. Copy this add‑on folder (e.g. `automatic-anki/`) to:
   - **Windows**: `%APPDATA%\Anki2\addons21\`
   - **macOS**: `~/Library/Application Support/Anki2/addons21/`
   - **Linux**: `~/.local/share/Anki2/addons21/`
3. Start Anki. You should see a new menu item:
   - **Tools → Generate Anki cards from PDF**
   - **Tools → Check PDF Renderer (automatic-anki)** (optional diagnostics)

> The add‑on supports Anki 2.1.x (Qt6) and tries **PyMuPDF** first for PDF rendering; it can fall back to Qt PDF or embedded images when available.

---

## Quick Start

1. **Set API key** (first run)
   - When prompted, paste your **OpenAI API key**. It’s saved in add‑on config.
2. **Run generation**
   - **Tools → Generate Anki cards from PDF**
   - Pick your PDF (lecture slides / notes).
3. **Choose options**
   - **Card types**: check **Basic** and/or **Cloze**.
   - **Cards per slide**:
     - **AI (all)** – keep all returned cards
     - **Range (min–max)** – random slice per page (Cloze kept first)
4. **Result**
   - A deck named after the PDF filename is created.
   - Cards are inserted with the slide image on the back.

---

## Configuration

The add‑on stores minimal settings in Anki’s add‑on config (automatic):
- `openai_api_key`: your key
- `types_basic`: `true/false`
- `types_cloze`: `true/false`
- `per_slide_mode`: `"ai"` or `"range"`
- `per_slide_min` / `per_slide_max`: integers (1–50)

You can change these from **Tools → Generate Anki cards from PDF** (Options dialog), or via Anki’s **Add‑ons → Config** UI.

---

## How It Works (Architecture)

**One responsibility per layer**:

## How It Works (Architecture)

PDF
 └─▶ main.py
      ├─ calls pdf_parser.extract_text_from_pdf() to get per‑page text
      ├─ calls openai_cards.py for each page:
      │     • BASIC → question/answer cards
      │     • CLOZE → declarative cards with {{c1::…}}
      ├─ routes cards by presence of {{c}}
      │     • {{c}} present → Cloze + Slide
      │     • otherwise     → Basic + Slide
      ├─ attaches slide image to each note
      └─ force‑moves cards to the PDF‑named deck

Anki Templates & CSS
 └─ control layout, styling, cloze color, and images

