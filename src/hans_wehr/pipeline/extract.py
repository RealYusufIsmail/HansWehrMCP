"""
src/pipeline/extract.py
-----------------------
Stage 1 of the pipeline: extract text and full font metadata from the
Hans Wehr PDF using PyMuPDF (fitz).

Output: one JSON file per page at data/raw/page_NNNN.json.

Each JSON file has the structure:
{
  "page_number": 42,
  "width": 595.0,
  "height": 841.0,
  "blocks": [
    {
      "block_no": 0,
      "bbox": [x0, y0, x1, y1],
      "lines": [
        {
          "line_no": 0,
          "bbox": [x0, y0, x1, y1],
          "spans": [
            {
              "text": "كَتَبَ",
              "font": "ArabicNaskhMT-Bold",
              "size": 12.0,
              "flags": 20,           // PyMuPDF font flags bitmask
              "is_bold": true,
              "is_italic": false,
              "color": 0,            // RGB int
              "bbox": [x0, y0, x1, y1]
            }
          ]
        }
      ]
    }
  ]
}

Font flags bitmask (PyMuPDF):
  bit 0 (1)  = superscript
  bit 1 (2)  = italic
  bit 2 (4)  = serifed
  bit 3 (8)  = monospaced
  bit 4 (16) = bold

Usage via typer CLI:
  python -m src.pipeline.extract --pdf data/hans_wehr.pdf --out data/raw/
  python -m src.pipeline.extract --pdf data/hans_wehr.pdf --out data/raw/ --dry-run
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import fitz  # PyMuPDF
import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn

app = typer.Typer(help="Extract text + font metadata from the Hans Wehr PDF.")
console = Console()
log = logging.getLogger(__name__)

# Pages to process in dry-run mode (1-indexed, inclusive).
DRY_RUN_PAGES = (1, 20)

# Minimum font size (pt) that we ever expect meaningful text at.
MIN_FONT_SIZE = 4.0


def _flags_to_bools(flags: int) -> tuple[bool, bool]:
    """Decode PyMuPDF span flags bitmask → (is_bold, is_italic)."""
    is_bold = bool(flags & (1 << 4))    # bit 4
    is_italic = bool(flags & (1 << 1))  # bit 1
    return is_bold, is_italic


def extract_page(page: fitz.Page, page_number: int) -> dict:
    """Extract all text spans with font metadata from a single PDF page.

    Uses get_text("rawdict") which gives per-span font details including size,
    flags (bold/italic), font name, and colour.

    Returns a page dict matching the schema documented at the top of this file.
    """
    raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)

    blocks_out: list[dict] = []
    for block in raw.get("blocks", []):
        # Skip image blocks — we only want text
        if block.get("type") != 0:
            continue

        lines_out: list[dict] = []
        for line_no, line in enumerate(block.get("lines", [])):
            spans_out: list[dict] = []
            for span in line.get("spans", []):
                text = span.get("text", "")
                if not text.strip():
                    continue  # skip whitespace-only spans

                size = span.get("size", 0.0)
                if size < MIN_FONT_SIZE:
                    continue  # skip noise artefacts

                flags = span.get("flags", 0)
                is_bold, is_italic = _flags_to_bools(flags)

                spans_out.append({
                    "text": text,
                    "font": span.get("font", ""),
                    "size": round(size, 2),
                    "flags": flags,
                    "is_bold": is_bold,
                    "is_italic": is_italic,
                    "color": span.get("color", 0),
                    "bbox": [round(v, 2) for v in span.get("bbox", [0, 0, 0, 0])],
                })

            if spans_out:
                lines_out.append({
                    "line_no": line_no,
                    "bbox": [round(v, 2) for v in line.get("bbox", [0, 0, 0, 0])],
                    "spans": spans_out,
                })

        if lines_out:
            blocks_out.append({
                "block_no": block.get("number", 0),
                "bbox": [round(v, 2) for v in block.get("bbox", [0, 0, 0, 0])],
                "lines": lines_out,
            })

    return {
        "page_number": page_number,
        "width": round(page.rect.width, 2),
        "height": round(page.rect.height, 2),
        "blocks": blocks_out,
    }


def _page_output_path(out_dir: Path, page_number: int) -> Path:
    """e.g. data/raw/page_0042.json"""
    return out_dir / f"page_{page_number:04d}.json"


def run_extraction(
    pdf_path: Path,
    out_dir: Path,
    start_page: int,
    end_page: int | None,
    overwrite: bool,
) -> int:
    """Extract pages [start_page, end_page] (1-indexed, inclusive).

    Returns the number of pages successfully written.
    """
    if not pdf_path.exists():
        console.print(f"[bold red]Error:[/bold red] PDF not found at {pdf_path}")
        sys.exit(2)

    out_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count
    log.info("Opened PDF: %d pages", total_pages)

    # Convert to 0-indexed for PyMuPDF
    first = max(0, start_page - 1)
    last = min(total_pages - 1, (end_page or total_pages) - 1)
    page_range = range(first, last + 1)

    written = 0
    errors = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Extracting pages {start_page}–{end_page or total_pages}",
            total=len(page_range),
        )

        for page_idx in page_range:
            page_number = page_idx + 1  # 1-indexed for humans
            out_path = _page_output_path(out_dir, page_number)

            if out_path.exists() and not overwrite:
                log.debug("Skipping page %d — already extracted", page_number)
                progress.advance(task)
                continue

            try:
                page = doc[page_idx]
                page_data = extract_page(page, page_number)

                # Warn if page yielded no text (possibly a scanned image page)
                if not page_data["blocks"]:
                    log.warning("Page %d: no text blocks found — may be a scanned image", page_number)

                out_path.write_text(json.dumps(page_data, ensure_ascii=False, indent=2), encoding="utf-8")
                written += 1

            except Exception as exc:
                log.error("Page %d: extraction failed — %s", page_number, exc)
                errors += 1

            progress.advance(task)

    doc.close()
    console.print(
        f"[green]Done.[/green] Written {written} pages, {errors} errors. "
        f"Output: {out_dir}"
    )
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def main(
    pdf: Path = typer.Option(
        Path("data/hans_wehr.pdf"),
        "--pdf", "-p",
        help="Path to the Hans Wehr PDF.",
        exists=True,
        file_okay=True,
        dir_okay=False,
    ),
    out: Path = typer.Option(
        Path("data/raw"),
        "--out", "-o",
        help="Output directory for per-page JSON files.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=f"Process only pages {DRY_RUN_PAGES[0]}–{DRY_RUN_PAGES[1]} for validation.",
    ),
    start_page: int = typer.Option(
        1,
        "--start",
        help="First page to extract (1-indexed). Ignored in --dry-run.",
    ),
    end_page: int | None = typer.Option(
        None,
        "--end",
        help="Last page to extract (1-indexed, inclusive). Default: last page.",
    ),
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Re-extract pages even if the output JSON already exists.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable DEBUG logging."),
) -> None:
    """Extract text and font metadata from the Hans Wehr PDF.

    Outputs one JSON file per page to the --out directory.
    Use --dry-run to process only the first 20 pages before committing to a full run.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if dry_run:
        console.print(
            f"[yellow]Dry-run mode:[/yellow] processing pages "
            f"{DRY_RUN_PAGES[0]}–{DRY_RUN_PAGES[1]} only."
        )
        run_extraction(pdf, out, DRY_RUN_PAGES[0], DRY_RUN_PAGES[1], overwrite)
    else:
        run_extraction(pdf, out, start_page, end_page, overwrite)


if __name__ == "__main__":
    app()
