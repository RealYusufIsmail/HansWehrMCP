# hans-wehr-mcp

An MCP (Model Context Protocol) server that exposes the Hans Wehr *Dictionary of Modern Written Arabic* (4th edition) as queryable tools for LLM clients.

Once set up, any MCP-compatible client — Claude Desktop, Continue.dev, or a custom agent — can call `lookup_root("كتب")` and get back all entries under that root, complete with definitions, verb forms, plural patterns, and source page numbers.

See [SPEC.md](SPEC.md) for the full architecture, data model, and accuracy strategy.

---

## Requirements

- Python 3.12+
- [`uv`](https://github.com/astral-sh/uv) for dependency management
- The Hans Wehr 4th edition PDF (not included)
- An Anthropic API key (optional — only needed for the LLM refinement pass)

---

## Setup

### 1. Install dependencies

```bash
uv sync
```

### 2. Place the PDF

Copy your Hans Wehr 4th edition PDF to:

```
data/hans_wehr.pdf
```

The file is gitignored and never committed.

### 3. Run the extraction pipeline

**Dry run first** (processes pages 1–20 only, fast):

```bash
uv run hans-extract --dry-run
```

Review the output in `data/raw/page_0001.json` … `page_0020.json`. Each file contains the raw text with font metadata. If the text looks garbled, your PDF may be a scanned image — see the [Troubleshooting](#troubleshooting) section.

**Full extraction** (takes several minutes for ~900 pages):

```bash
uv run hans-extract
```

### 4. Parse extracted text into structured entries

```bash
uv run hans-parse
```

Output: `data/processed/entries.jsonl` — one JSON dict per entry.

Check how many entries need review:

```bash
grep '"needs_review": true' data/processed/entries.jsonl | wc -l
```

### 5. Run the LLM refinement pass (optional)

This step sends low-confidence entries to `claude-sonnet-4-20250514` to correct parsing errors. It is optional but improves accuracy on tricky entries.

Copy `.env.example` to `.env` and add your key:

```bash
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-ant-...
```

**Dry run** (cost estimate only, no API calls):

```bash
uv run hans-refine --dry-run
```

The tool will print an estimated cost table. For a typical run (~2,000 low-confidence entries) expect **$1–$3**.

**Full refinement**:

```bash
uv run hans-refine
```

You will be prompted to confirm before any API calls are made.

Output: `data/processed/entries_refined.jsonl`

### 6. Import into SQLite

```bash
uv run hans-import
```

This creates `data/hans_wehr.db`, inserts all entries, builds FTS5 indexes, and resolves cross-references.

After import, run the validation report:

```bash
uv run hans-validate
```

You should see:

```
Root count:   PASS  13,050 (expected 12,350–13,650)
Entry count:  PASS  61,200 (expected 54,000–66,000)
XRef resolution: 92.3%
```

### 7. Start the MCP server

```bash
uv run hans-mcp
```

The server communicates over stdin/stdout using the MCP protocol. Leave this running while you configure your client.

---

## Claude Desktop configuration

Add to `~/.config/claude/claude_desktop_config.json` (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hans-wehr": {
      "command": "uv",
      "args": ["run", "hans-mcp"],
      "cwd": "/absolute/path/to/hans-wehr-mcp",
      "env": {
        "HANS_WEHR_DB_PATH": "/absolute/path/to/hans-wehr-mcp/data/hans_wehr.db"
      }
    }
  }
}
```

Restart Claude Desktop. You should see "hans-wehr" listed under connected MCP servers.

Try asking Claude: *"Look up the Arabic root كتب in Hans Wehr"*.

---

## Available MCP tools

| Tool | Description |
|---|---|
| `lookup_root(root)` | All entries under a root. Accepts Arabic script or transliteration. |
| `search_arabic(query, limit?)` | FTS search across Arabic words. Strips diacritics before searching. |
| `search_english(query, limit?)` | FTS search across English definitions (Porter-stemmed). |
| `get_entry(entry_id)` | Full detail for one entry including cross-references and parse metadata. |
| `list_roots(letter)` | All roots beginning with a given letter. |

---

## Running tests

```bash
uv run pytest
```

For coverage:

```bash
uv run pytest --cov=src --cov-report=term-missing
```

---

## Project structure

```
hans-wehr-mcp/
├── src/
│   ├── pipeline/
│   │   ├── extract.py      # Stage 1: PDF → per-page JSON (PyMuPDF)
│   │   ├── parser.py       # Stage 2: JSON → structured entry dicts
│   │   └── llm_refine.py   # Stage 3: LLM cleanup of low-confidence entries
│   ├── db/
│   │   ├── schema.sql      # SQLite schema with FTS5 virtual tables
│   │   └── queries.py      # All DB read queries (parameterised, injection-safe)
│   ├── mcp/
│   │   └── server.py       # MCP server and tool definitions
│   └── validation/
│       ├── sample_check.py # Random sample vs PDF page screenshot
│       ├── root_count.py   # Count validation against expected ranges
│       ├── xref_check.py   # Cross-reference resolution
│       └── report.py       # Aggregate accuracy report
├── scripts/
│   └── import_db.py        # Bulk import JSONL → SQLite
├── tests/
│   ├── test_parser.py      # Parser unit tests (no PDF required)
│   ├── test_queries.py     # DB query tests (in-memory SQLite)
│   └── test_mcp_server.py  # MCP tool dispatch tests
├── data/
│   ├── raw/                # Per-page JSON (gitignored)
│   └── processed/          # Structured JSONL (gitignored)
├── SPEC.md                 # Full architecture and accuracy specification
├── pyproject.toml
└── README.md
```

---

## Transliteration scheme

Hans Wehr uses the ALA-LC Arabic transliteration. Special characters that appear in the database:

| Arabic | Transliteration |
|---|---|
| ء | ʾ |
| ع | ʿ |
| ح | ḥ |
| خ | ḫ |
| ص | ṣ |
| ض | ḍ |
| ط | ṭ |
| ظ | ẓ |
| Long vowels | ā, ī, ū |

When querying tools, you can use either Arabic script or transliteration.

---

## Troubleshooting

**"No text blocks found" warnings during extraction**

Your PDF may contain scanned images rather than embedded text. PyMuPDF cannot extract text from rasterised pages. You would need to run OCR (e.g. with Tesseract + Arabic model) as a pre-processing step.

**Low root count after import**

The parser detects roots by font size and boldness. If your PDF uses non-standard fonts or has unusual rendering, you may need to adjust `ROOT_FONT_SIZE_MIN` and `DERIVED_FONT_SIZE_MIN` in `src/pipeline/parser.py` after inspecting the extracted JSON for a few representative pages.

**FTS search returns no results**

FTS5 indexes are built during import. If you inserted rows directly without going through `import_db.py`, rebuild the indexes by running:

```bash
uv run hans-import --dry-run  # safe — won't insert duplicates
# then run the actual import which rebuilds FTS at the end
```

**MCP server not appearing in Claude Desktop**

Check that the `cwd` and `HANS_WEHR_DB_PATH` in your `claude_desktop_config.json` are absolute paths. Relative paths are not resolved correctly by the MCP host.
