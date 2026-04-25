"""
src/validation/xref_check.py
-----------------------------
Attempt to resolve all unresolved cross-references in the cross_references table.

A cross-reference is a row where:
  resolved = 0
  to_arabic_raw  = the raw Arabic text of the link target

Resolution strategy:
  1. Exact match on entries.arabic_unvoweled
  2. Exact match on entries.arabic (with diacritics)
  3. Prefix match (first 3 chars of unvoweled form)
  4. If still unresolved, leave as is and count it as a miss

After resolution, prints a report:
  - % resolved
  - Common unresolved targets (may indicate parsing errors)
  - Writes updated resolved flags to the DB

Usage:
  python -m src.validation.xref_check --db data/hans_wehr.db
  python -m src.validation.xref_check --db data/hans_wehr.db --dry-run
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from hans_wehr.db.queries import get_connection, strip_diacritics

app = typer.Typer(help="Resolve cross-references and report resolution rate.")
console = Console()
log = logging.getLogger(__name__)


def _resolve_xrefs(conn, dry_run: bool) -> dict:
    """Attempt resolution of all unresolved cross-refs. Returns stats dict."""
    import sqlite3

    unresolved = conn.execute(
        "SELECT id, from_entry_id, to_arabic_raw FROM cross_references WHERE resolved = 0"
    ).fetchall()

    total = len(unresolved)
    resolved_count = 0
    still_unresolved: list[str] = []

    for row in unresolved:
        xref_id = row["id"]
        raw = row["to_arabic_raw"].strip() if row["to_arabic_raw"] else ""
        if not raw:
            still_unresolved.append("<empty>")
            continue

        bare = strip_diacritics(raw)

        # Strategy 1: exact unvoweled match
        match = conn.execute(
            "SELECT id FROM entries WHERE arabic_unvoweled = ? LIMIT 1",
            (bare,),
        ).fetchone()

        # Strategy 2: exact voweled match
        if match is None:
            match = conn.execute(
                "SELECT id FROM entries WHERE arabic = ? LIMIT 1",
                (raw,),
            ).fetchone()

        # Strategy 3: prefix match (first 4 chars — be conservative)
        if match is None and len(bare) >= 4:
            match = conn.execute(
                "SELECT id FROM entries WHERE arabic_unvoweled LIKE ? LIMIT 1",
                (bare[:4] + "%",),
            ).fetchone()

        if match:
            if not dry_run:
                conn.execute(
                    "UPDATE cross_references SET to_entry_id = ?, resolved = 1 WHERE id = ?",
                    (match["id"], xref_id),
                )
            resolved_count += 1
        else:
            still_unresolved.append(bare)

    if not dry_run:
        conn.commit()

    unresolved_counts = Counter(still_unresolved)
    return {
        "total": total,
        "resolved": resolved_count,
        "still_unresolved": total - resolved_count,
        "resolution_rate": round(resolved_count / max(total, 1) * 100, 2),
        "top_unresolved": unresolved_counts.most_common(20),
    }


@app.command()
def main(
    db: Path = typer.Option(Path("data/hans_wehr.db"), "--db"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report without writing to DB."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Resolve cross-references and print a resolution rate report."""
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)

    if not db.exists():
        console.print(f"[bold red]Error:[/bold red] DB not found: {db}")
        raise typer.Exit(2)

    # Need a read-write connection for this script
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")

    stats = _resolve_xrefs(conn, dry_run)
    conn.close()

    table = Table(title="Cross-Reference Resolution", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("Total cross-refs processed", str(stats["total"]))
    table.add_row("Resolved", str(stats["resolved"]))
    table.add_row("Still unresolved", str(stats["still_unresolved"]))
    table.add_row("Resolution rate", f"{stats['resolution_rate']}%")
    console.print(table)

    if stats["top_unresolved"]:
        console.print("\n[bold]Top unresolved targets (may indicate parsing errors):[/bold]")
        miss_table = Table(show_header=True)
        miss_table.add_column("Target (unvoweled)", style="yellow")
        miss_table.add_column("Count", style="magenta")
        for target, count in stats["top_unresolved"]:
            miss_table.add_row(target, str(count))
        console.print(miss_table)

    if dry_run:
        console.print("\n[yellow]Dry-run mode — no changes written to DB.[/yellow]")
    else:
        console.print(f"\n[green]Updated {stats['resolved']} cross-references in DB.[/green]")


if __name__ == "__main__":
    app()
