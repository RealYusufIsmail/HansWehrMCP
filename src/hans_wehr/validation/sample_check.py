"""
src/validation/sample_check.py
--------------------------------
Sample 100 random entries from the DB and render the corresponding PDF page
as a PNG thumbnail, then print the entry's definition alongside it for
side-by-side human verification.

This is the primary ground-truth accuracy check: a human reads the printed
definition on the page image and compares it to what the parser captured.

Usage:
  python -m src.validation.sample_check --db data/hans_wehr.db --pdf data/hans_wehr.pdf
  python -m src.validation.sample_check --n 20 --out validation_samples/
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import fitz  # PyMuPDF
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hans_wehr.db.queries import get_connection, get_random_entries, parse_plural_forms

app = typer.Typer(help="Sample random entries and compare against PDF pages.")
console = Console()
log = logging.getLogger(__name__)

# How many pixels wide to render the page thumbnail
RENDER_DPI = 150  # 150 DPI gives a readable thumbnail


def render_page_as_png(pdf_path: Path, page_number: int, out_path: Path) -> None:
    """Render a single PDF page as a PNG file at RENDER_DPI."""
    doc = fitz.open(str(pdf_path))
    # page_number is 1-indexed
    page = doc[page_number - 1]
    mat = fitz.Matrix(RENDER_DPI / 72, RENDER_DPI / 72)  # 72 pt = 1 inch
    pix = page.get_pixmap(matrix=mat)
    pix.save(str(out_path))
    doc.close()


@app.command()
def main(
    db: Path = typer.Option(Path("data/hans_wehr.db"), "--db", help="Path to the SQLite DB."),
    pdf: Path = typer.Option(Path("data/hans_wehr.pdf"), "--pdf", help="Path to the PDF."),
    n: int = typer.Option(100, "--n", help="Number of entries to sample."),
    out: Path = typer.Option(
        Path("data/validation_samples"),
        "--out",
        help="Directory to write page thumbnail PNGs.",
    ),
    no_images: bool = typer.Option(
        False, "--no-images", help="Skip rendering PNG thumbnails (text report only)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Randomly sample entries and produce a side-by-side accuracy report."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)

    if not db.exists():
        console.print(f"[bold red]Error:[/bold red] DB not found: {db}")
        raise typer.Exit(2)

    if not no_images and not pdf.exists():
        console.print(f"[bold red]Error:[/bold red] PDF not found: {pdf}. Use --no-images to skip.")
        raise typer.Exit(2)

    out.mkdir(parents=True, exist_ok=True)

    with get_connection(str(db)) as conn:
        entries = get_random_entries(conn, n)

    console.print(f"Sampled {len(entries)} entries.\n")

    results = []
    for entry in entries:
        entry_dict = dict(entry)
        page_number = entry_dict["page_number"]
        entry_id = entry_dict["id"]

        png_path = out / f"page_{page_number:04d}.png"
        if not no_images and not png_path.exists():
            try:
                render_page_as_png(pdf, page_number, png_path)
            except Exception as exc:
                log.warning("Could not render page %d: %s", page_number, exc)

        results.append({
            "entry_id": entry_id,
            "arabic": entry_dict.get("arabic", ""),
            "transliteration": entry_dict.get("transliteration", ""),
            "definition": entry_dict.get("definition", ""),
            "confidence": round(float(entry_dict.get("confidence", 0)), 2),
            "page_number": page_number,
            "needs_review": bool(entry_dict.get("needs_review")),
            "png_path": str(png_path) if not no_images else None,
        })

    # Print a rich table summary
    table = Table(title=f"Sample of {len(results)} Entries", show_lines=True)
    table.add_column("ID", style="dim", width=6)
    table.add_column("Arabic", style="bold")
    table.add_column("Translit", style="cyan")
    table.add_column("Definition (first 60 chars)", style="white")
    table.add_column("Conf.", style="magenta", width=6)
    table.add_column("Page", style="dim", width=5)
    table.add_column("Review?", style="yellow", width=8)

    for r in results:
        defn_preview = (r["definition"] or "")[:60]
        if len(r["definition"] or "") > 60:
            defn_preview += "…"
        table.add_row(
            str(r["entry_id"]),
            r["arabic"],
            r["transliteration"] or "",
            defn_preview,
            str(r["confidence"]),
            str(r["page_number"]),
            "YES" if r["needs_review"] else "",
        )

    console.print(table)

    # Write machine-readable report
    report_path = out / "sample_report.json"
    report_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    console.print(f"\nDetailed report written to {report_path}")
    if not no_images:
        console.print(f"Page thumbnails in {out}/ — open alongside the report for manual review.")


if __name__ == "__main__":
    app()
