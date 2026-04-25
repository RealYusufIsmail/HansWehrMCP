# Hans Wehr MCP — Project Specification

## 1. Overview

`hans-wehr-mcp` converts the Hans Wehr *A Dictionary of Modern Written Arabic* (4th edition PDF, ~900 pages) into a structured SQLite database and exposes it as an MCP (Model Context Protocol) server. Any MCP-compatible client — Claude Desktop, Continue.dev, custom agents — can query the dictionary with Arabic or English terms at inference time.

**Core values:**
- Accuracy over completeness: every parsed entry carries a confidence score; nothing is silently dropped.
- Full audit trail: every entry links back to its source PDF page.
- RTL-safe throughout: all Arabic text stored as UTF-8 with preserved diacritics; indexed both with and without diacritics.

---

## 2. Architecture

```
data/hans_wehr.pdf
        │
        ▼
┌─────────────────────────┐
│   src/pipeline/extract.py│  PyMuPDF → per-page JSON with font metadata
└────────────┬────────────┘
             │  data/raw/page_NNNN.json
             ▼
┌─────────────────────────┐
│   src/pipeline/parser.py │  Structural parser → entry dicts + confidence scores
└────────────┬────────────┘
             │  data/processed/entries.jsonl
             ▼
┌──────────────────────────────┐
│  src/pipeline/llm_refine.py  │  Anthropic API cleanup of low-confidence entries
└────────────┬─────────────────┘
             │  data/processed/entries_refined.jsonl
             ▼
┌─────────────────────────┐
│   scripts/import_db.py   │  Bulk-insert into SQLite; build FTS indexes
└────────────┬────────────┘
             │  data/hans_wehr.db
             ▼
┌─────────────────────────┐
│    src/mcp/server.py     │  MCP server exposing lookup tools
└─────────────────────────┘
```

### Component responsibilities

| Component | Input | Output | Key concern |
|---|---|---|---|
| `extract.py` | PDF file | `data/raw/page_NNNN.json` | Preserve font size, weight, flags per span |
| `parser.py` | Raw page JSON | `data/processed/entries.jsonl` | Root/entry/definition segmentation; confidence |
| `llm_refine.py` | Low-confidence entries | `data/processed/entries_refined.jsonl` | Structured correction via Claude; cost control |
| `import_db.py` | Refined JSONL | `hans_wehr.db` | Schema enforcement; FTS population |
| `server.py` | SQLite DB | MCP tool responses | Query routing; confidence surfacing |

---

## 3. Data Model

### 3.1 Roots

The dictionary is organised around Arabic tri-literal (and some quad-literal) roots. Each root is a headword printed in **bold, larger font** in the PDF.

Fields:
- `id` INTEGER PRIMARY KEY
- `arabic` TEXT — root in Arabic script, UTF-8, with tashkeel if present (e.g. كَتَبَ)
- `arabic_unvoweled` TEXT — root stripped of diacritics, for flexible lookup (e.g. كتب)
- `transliteration` TEXT — Hans Wehr academic transliteration (e.g. `kataba`)
- `page_number` INTEGER — PDF page where this root first appears
- `entry_count` INTEGER — denormalised count of child entries (updated on import)

### 3.2 Entries

A single lexical item: a derived verb form, noun, adjective, or phrase under a root.

Fields:
- `id` INTEGER PRIMARY KEY
- `root_id` INTEGER FK → roots
- `arabic` TEXT — the entry word(s) in Arabic script
- `arabic_unvoweled` TEXT — stripped of diacritics
- `transliteration` TEXT
- `part_of_speech` TEXT — `verb`, `noun`, `adjective`, `adverb`, `particle`, `phrase`, `proper_noun`
- `verb_form` TEXT NULLABLE — Roman numeral I–X (Hans Wehr verb form notation)
- `plural_forms` TEXT — JSON array of plural forms e.g. `["كُتُب", "أَكْتَاب"]`
- `definition` TEXT — full English definition text
- `grammar_notes` TEXT NULLABLE — e.g. "with acc.", "foll. by bi-"
- `page_number` INTEGER — source PDF page
- `confidence` REAL — 0.0–1.0, how cleanly this entry was parsed
- `needs_review` INTEGER — boolean flag, 1 if confidence < 0.75 or LLM flagged it

### 3.3 Cross-references

Hans Wehr frequently cross-references entries with "see →" and "cf." markers.

Fields:
- `id` INTEGER PRIMARY KEY
- `from_entry_id` INTEGER FK → entries
- `to_entry_id` INTEGER FK → entries NULLABLE — NULL until resolved in post-processing
- `to_arabic_raw` TEXT — raw Arabic text of the target (before resolution)
- `ref_type` TEXT — `see`, `cf`, `plural_of`, `root_of`, `variant_of`
- `resolved` INTEGER — 1 if `to_entry_id` was successfully matched

### 3.4 Parse Metadata

One row per entry, recording extraction provenance.

Fields:
- `entry_id` INTEGER FK → entries
- `raw_text` TEXT — verbatim extracted text before any parsing
- `parse_method` TEXT — `structural` | `llm_refined` | `manual`
- `llm_model` TEXT NULLABLE — model used for refinement, e.g. `claude-sonnet-4-20250514`
- `confidence` REAL — mirrors entries.confidence
- `extraction_warnings` TEXT — JSON array of warning strings
- `created_at` TEXT — ISO 8601 timestamp

---

## 4. MCP Tools

All tools return JSON. Confidence scores are always included in responses so the client can signal uncertainty.

### 4.1 `lookup_root`

**Description:** Return all entries under a root. The most common dictionary query.

**Input:**
```json
{
  "root": "كتب"          // Arabic script OR transliteration e.g. "ktb" or "kataba"
}
```

**Output:**
```json
{
  "root": { "arabic": "كَتَبَ", "transliteration": "kataba", "page": 812 },
  "entries": [
    {
      "id": 4521,
      "arabic": "كَتَبَ",
      "transliteration": "kataba",
      "part_of_speech": "verb",
      "verb_form": "I",
      "definition": "to write; to correspond (with s.o.)...",
      "confidence": 0.97
    }
  ],
  "total": 43
}
```

### 4.2 `search_arabic`

**Description:** Full-text search across Arabic words (FTS5). Strips diacritics before searching.

**Input:**
```json
{ "query": "كتاب", "limit": 20 }
```

**Output:** Array of entry summaries with match snippets and confidence scores.

### 4.3 `search_english`

**Description:** Full-text search across English definitions (FTS5).

**Input:**
```json
{ "query": "to write a letter", "limit": 20 }
```

**Output:** Array of entry summaries with match snippets and confidence scores.

### 4.4 `get_entry`

**Description:** Full detail for a single entry by ID.

**Input:**
```json
{ "entry_id": 4521 }
```

**Output:** Complete entry record including definition, grammar notes, plural forms, cross-references, parse metadata, and source page number.

### 4.5 `list_roots`

**Description:** All roots beginning with a given Arabic letter (or transliteration letter).

**Input:**
```json
{ "letter": "ك" }    // or "k"
```

**Output:** Array of root summaries with entry counts, sorted by page number.

---

## 5. Transliteration Scheme

Hans Wehr uses the standard Arabic–English transliteration adopted by the Library of Congress / ALA-LC. The following characters appear and must be stored and displayed exactly:

| Arabic | Transliteration | Unicode codepoint |
|---|---|---|
| ء | ʾ (hamza) | U+02BE |
| ع | ʿ (ayin) | U+02BF |
| ح | ḥ | U+1E25 |
| خ | ḫ | U+1E2B |
| ص | ṣ | U+1E63 |
| ض | ḍ | U+1E0D |
| ط | ṭ | U+1E6D |
| ظ | ẓ | U+1E93 |
| غ | ġ | U+0121 |
| ق | q | — |
| ش | š | U+0161 |
| ث | ṯ | U+1E6F |
| ذ | ḏ | U+1E0F |
| ā | ā (long a) | U+0101 |
| ī | ī (long i) | U+012B |
| ū | ū (long u) | U+016B |

**Implementation rule:** Transliteration strings are stored as-is. Do not normalise or simplify. When accepting transliteration input in MCP tools, also accept ASCII approximations (e.g. `h.` for `ḥ`) and map them before lookup.

---

## 6. Accuracy Strategy

### 6.1 Confidence Scoring

The parser assigns a floating-point confidence score (0.0–1.0) to each entry based on:

| Signal | Score deduction |
|---|---|
| Root detected by expected bold+size font | 0 (baseline) |
| Definition text contains unrecognised Unicode ranges | −0.10 |
| Part-of-speech tag not found in expected positions | −0.15 |
| Entry spans multiple pages (broken across page break) | −0.10 |
| Arabic text detected but no transliteration follows | −0.15 |
| Verb form numeral not in I–X range | −0.05 |
| Plural form bracket not closed | −0.10 |

Entries with confidence < 0.75 are flagged `needs_review = 1` and queued for LLM refinement.

### 6.2 LLM Refinement Pass

`llm_refine.py` sends batches of low-confidence entries to `claude-sonnet-4-20250514` with:
- The raw extracted text
- The page image (base64 PNG thumbnail)
- A structured output schema

The LLM response replaces the structurally-parsed fields but the original raw text and parse method are preserved in `parse_metadata`. Refinement is idempotent: re-running compares the new response to the stored one and only writes if the result differs.

### 6.3 Human Review Queue

A `needs_review = 1` flag is queryable via the MCP server. A future CLI command (`scripts/review_cli.py`) can present entries for manual correction.

### 6.4 Root Count Validation

The Hans Wehr 4th edition contains approximately **13,000 roots** and **~60,000 entries**. After import, the validation suite checks:
- Root count is within 5% of 13,000
- Entry count is within 5% of 60,000
- No root has zero entries
- All cross-reference targets resolve (>90% expected)

### 6.5 Sample Comparison

`src/validation/sample_check.py` randomly selects 100 entries and renders the corresponding PDF page as a PNG (via PyMuPDF), then displays the entry definition alongside the page image for manual side-by-side review. This is the ground-truth check.

---

## 7. Arabic Text Handling

### 7.1 Storage
- Always UTF-8.
- Store both voweled (`arabic`) and unvoweled (`arabic_unvoweled`) forms.
- Unvoweled form computed by stripping Unicode ranges U+064B–U+065F (tashkeel/harakat) and U+0670 (superscript alef).

### 7.2 Indexing
- FTS5 table `entries_arabic_fts` indexes `arabic_unvoweled` — searches always strip input diacritics before querying.
- FTS5 table `entries_english_fts` indexes `definition`.

### 7.3 RTL Display
The MCP server does not add directional marks; consumers are responsible for rendering. However, all Arabic strings in JSON responses are tagged with their script in a companion `script` field for consumers that need it.

---

## 8. Error Handling

| Scenario | Behaviour |
|---|---|
| PDF page yields no text (scanned image) | Log warning; emit a stub entry with `confidence=0.0`, `needs_review=1` |
| PyMuPDF can't open PDF | Raise with clear message; exit code 2 |
| Anthropic API rate-limit | Exponential backoff, max 5 retries; log cost consumed so far |
| DB entry violates FK constraint | Log with page number; skip insertion; write to `data/failed_entries.jsonl` |
| MCP tool receives unresolvable query | Return `{"error": "not_found", "query": "..."}` — never raise exception to client |

---

## 9. Project Conventions

- Python 3.12+, managed with `uv`.
- All CLI entry points use `typer`; progress output uses `rich`.
- Tests in `pytest`; target coverage >80% on parser and db modules.
- No environment variables stored in code; use a `.env` file (gitignored) for `ANTHROPIC_API_KEY`.
- Type annotations on all public functions.
- Logging via Python `logging` module at `INFO` by default; `DEBUG` with `--verbose`.
