# Setup Guide

This guide walks you through building the Hans Wehr dictionary database and
connecting it to Claude Desktop. Follow the path that matches your setup.

---

## Before you start

You need:
- A Mac running macOS 13 (Ventura) or later
- Python 3.12+ and `uv` installed
- The Hans Wehr 4th edition PDF

Install `uv` if you don't have it:
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Step 1 — Clone and install

```bash
git clone https://github.com/yourname/hans-wehr-mcp
cd hans-wehr-mcp
uv pip install -e ".[pipeline-local]"
```

Also install system tools (needed for OCR verification):
```bash
brew install tesseract tesseract-lang
brew install ollama
```

---

## Step 2 — Place your PDF

Copy your PDF into the project:
```bash
cp ~/Downloads/hans_wehr.pdf data/hans_wehr.pdf
```

---

## Step 3 — Check your PDF

This tells you whether your PDF has text you can copy-paste, or is a scanned image:
```bash
uv run hans-probe --pdf data/hans_wehr.pdf
```

Read the verdict at the bottom of the output, then follow the matching path below.

---

## Path A — PDF has selectable text (most common)

The probe reported **SELECTABLE** or **MIXED**.

### A1. Extract text from the PDF
```bash
uv run hans-extract --dry-run   # test on 20 pages first
uv run hans-extract             # full run (~5 min)
```

Skip to **Step 4**.

---

## Path B — PDF is a scanned image

The probe reported **SCANNED**.

### B1. OCR the PDF with Vision (free, on-device)
```bash
uv run hans-ocr --pdf data/hans_wehr.pdf --dry-run   # test on 20 pages
uv run hans-ocr --pdf data/hans_wehr.pdf             # full run (~30–60 min)
```

### B2. Verify OCR quality
This cross-checks Vision against Tesseract. Pages where they disagree get flagged
automatically so the LLM can fix them later.

```bash
uv run hans-verify-ocr --pdf data/hans_wehr.pdf --dry-run   # preview
uv run hans-verify-ocr --pdf data/hans_wehr.pdf             # full check
```

Look at the report. A healthy run shows average agreement above 70%.
Pages below 40% will be cleaned up by the LLM in Step 5.

---

## Step 4 — Parse the extracted text

Converts raw page JSON into structured dictionary entries:
```bash
uv run hans-parse --dry-run   # test on 20 pages
uv run hans-parse             # full run (~2–5 min)
```

Check how many entries need LLM cleanup:
```bash
grep '"needs_review": true' data/processed/entries.jsonl | wc -l
```

---

## Step 5 — LLM refinement (free, on-device)

Pull the model once, then start the server:
```bash
ollama pull qwen2.5:7b    # ~4 GB download, one time only
ollama serve              # leave this running in a second terminal tab
```

Run the refinement:
```bash
uv run hans-refine --local --dry-run   # shows count, no LLM calls
uv run hans-refine --local             # full run (speed depends on your Mac)
```

Typical times on Apple Silicon:
- M1/M2 (8 GB): ~2 entries/sec with qwen2.5:7b
- M2 Pro/Max (16 GB+): try `--model qwen2.5:14b` for better accuracy

---

## Step 6 — Build the database

```bash
uv run hans-import
```

Then resolve cross-references between entries:
```bash
uv run python scripts/resolve_xrefs.py
```

---

## Step 7 — Validate

Check the numbers look right:
```bash
uv run hans-validate
```

You should see something like:
```
Root count:    PASS  13,050  (expected 12,350–13,650)
Entry count:   PASS  61,200  (expected 51,000–69,000)
XRef resolution: 92.3%
```

If entry count is low, your PDF fonts may need threshold tuning — see the
Troubleshooting section in README.md.

---

## Step 8 — Connect to Claude Desktop

Open your Claude Desktop config file:
```bash
open "~/Library/Application Support/Claude/claude_desktop_config.json"
```

Add this (replace the path with your actual project path):
```json
{
  "mcpServers": {
    "hans-wehr": {
      "command": "uv",
      "args": ["run", "hans-mcp"],
      "cwd": "/Users/yourname/hans-wehr-mcp",
      "env": {
        "HANS_WEHR_DB_PATH": "/Users/yourname/hans-wehr-mcp/data/hans_wehr.db"
      }
    }
  }
}
```

Restart Claude Desktop. You should see **hans-wehr** in the connected servers list.

Try it:
> *"Look up the Arabic root كتب in Hans Wehr"*
> *"Search Hans Wehr for entries related to writing"*
> *"What does كِتَاب mean?"*

---

## Quick reference

| Command | What it does |
|---|---|
| `hans-probe` | Check if PDF has text or is scanned |
| `hans-extract` | Extract text from a selectable PDF |
| `hans-ocr` | OCR a scanned PDF using macOS Vision |
| `hans-verify-ocr` | Cross-check OCR quality with Tesseract |
| `hans-parse` | Convert page JSON → structured entries |
| `hans-refine --local` | Clean up low-confidence entries with Ollama |
| `hans-import` | Load entries into SQLite |
| `hans-validate` | Check counts and cross-reference resolution |
| `hans-mcp` | Start the MCP server |

---

## Something went wrong?

**Tesseract not found**
```bash
brew install tesseract tesseract-lang
tesseract --list-langs | grep ara   # should print "ara"
```

**Ollama not responding**
```bash
ollama serve   # start it in a separate terminal
ollama list    # check qwen2.5:7b is downloaded
```

**Not enough RAM for the model**
```bash
ollama pull qwen2.5:3b                        # smaller model
uv run hans-refine --local --model qwen2.5:3b
```

**OCR agreement is very low (< 40% on most pages)**

Try the high-quality Tesseract model:
```bash
# Download ara.traineddata from tesseract-ocr/tessdata_best on GitHub
# Place it in:
$(brew --prefix tesseract)/share/tessdata/
```

**Entry count is too low after import**

Run `hans-probe` again and read the font inventory table. Then adjust these
two constants in `src/hans_wehr/pipeline/parser.py` to match the sizes shown:
```python
ROOT_FONT_SIZE_MIN = 10.5    # bold Arabic headword
DERIVED_FONT_SIZE_MIN = 8.5  # bold derived form
```

Re-run `hans-parse` and `hans-import` after changing them.
