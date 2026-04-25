"""
src/validation/report.py
-------------------------
Aggregate accuracy report combining all validation checks.
Produces both a rich terminal report and a machine-readable JSON file.

Runs:
  1. Root/entry count check (root_count.py logic)
  2. Cross-reference resolution rate (xref_check.py logic)
  3. Confidence score distribution
  4. Parse method breakdown (structural vs llm_refined vs manual)

Usage:
  python -m src.validation.report --db data/hans_wehr.db
  python -m src.validation.report --db data/hans_wehr.db --json-out report.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.db.queries import (
    get_connection,
    count_roots,
    count_entries,
    count_needs_review,
    count_unresolved_xrefs,
)
from src.validation.root_count import EXPECTED_ROOTS, EXPECTED_ENTRIES, ROOT_TOLERANCE, ENTRY_TOLERANCE

app = typer.Typer(help="Generate a full accuracy report for the parsed dictionary database.")
console = Console()


def _pct(n: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{100 * n / total:.1f}%"


def build_report(db_path: Path) -> dict:
    """Collect all metrics and return a report dict."""
    with get_connection(str(db_path)) as conn:
        n_roots = count_roots(conn)
        n_entries = count_entries(conn)
        n_review = count_needs_review(conn)
        n_xref_unresolved = count_unresolved_xrefs(conn)
        n_xref_total = conn.execute("SELECT COUNT(*) FROM cross_references").fetchone()[0]

        # Confidence distribution
        conf_buckets = {}
        for threshold in [0.5, 0.6, 0.7, 0.75, 0.8, 0.9, 0.95, 1.0]:
            count = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE confidence >= ?", (threshold,)
            ).fetchone()[0]
            conf_buckets[f">={threshold}"] = count

        # Parse method breakdown
        parse_methods = conn.execute(
            "SELECT parse_method, COUNT(*) as cnt FROM parse_metadata GROUP BY parse_method"
        ).fetchall()
        parse_breakdown = {row["parse_method"]: row["cnt"] for row in parse_methods}

        # Average confidence
        avg_conf = conn.execute("SELECT AVG(confidence) FROM entries").fetchone()[0] or 0.0

        # Roots with zero entries
        zero_entry_roots = conn.execute(
            "SELECT COUNT(*) FROM roots WHERE entry_count = 0"
        ).fetchone()[0]

    # Compute pass/fail for key metrics
    root_lo = int(EXPECTED_ROOTS * (1 - ROOT_TOLERANCE))
    root_hi = int(EXPECTED_ROOTS * (1 + ROOT_TOLERANCE))
    entry_lo = int(EXPECTED_ENTRIES * (1 - ENTRY_TOLERANCE))
    entry_hi = int(EXPECTED_ENTRIES * (1 + ENTRY_TOLERANCE))

    checks = {
        "root_count_ok": root_lo <= n_roots <= root_hi,
        "entry_count_ok": entry_lo <= n_entries <= entry_hi,
        "zero_entry_roots_ok": zero_entry_roots == 0,
        "xref_resolution_ok": (n_xref_total == 0) or ((n_xref_total - n_xref_unresolved) / n_xref_total >= 0.90),
    }
    all_passed = all(checks.values())

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "summary": {
            "all_checks_passed": all_passed,
            "n_roots": n_roots,
            "n_entries": n_entries,
            "n_needs_review": n_review,
            "pct_needs_review": round(100 * n_review / max(n_entries, 1), 2),
            "avg_confidence": round(float(avg_conf), 4),
            "zero_entry_roots": zero_entry_roots,
        },
        "cross_references": {
            "total": n_xref_total,
            "unresolved": n_xref_unresolved,
            "resolved": n_xref_total - n_xref_unresolved,
            "resolution_rate_pct": round(
                100 * (n_xref_total - n_xref_unresolved) / max(n_xref_total, 1), 2
            ),
        },
        "confidence_distribution": conf_buckets,
        "parse_method_breakdown": parse_breakdown,
        "checks": checks,
        "expected_ranges": {
            "roots": {"min": root_lo, "max": root_hi},
            "entries": {"min": entry_lo, "max": entry_hi},
        },
    }


@app.command()
def main(
    db: Path = typer.Option(Path("data/hans_wehr.db"), "--db"),
    json_out: Path | None = typer.Option(None, "--json-out", help="Write report JSON to this file."),
) -> None:
    """Print an aggregated accuracy report and optionally write JSON."""
    if not db.exists():
        console.print(f"[bold red]Error:[/bold red] DB not found: {db}")
        raise typer.Exit(2)

    report = build_report(db)
    summary = report["summary"]
    xref = report["cross_references"]
    checks = report["checks"]

    # Overall status panel
    status_color = "green" if summary["all_checks_passed"] else "red"
    status_text = "ALL CHECKS PASSED" if summary["all_checks_passed"] else "SOME CHECKS FAILED"
    console.print(Panel(f"[bold {status_color}]{status_text}[/bold {status_color}]", title="Hans Wehr Accuracy Report"))

    # Key metrics table
    metrics = Table(show_header=True, header_style="bold cyan")
    metrics.add_column("Metric")
    metrics.add_column("Value")
    metrics.add_column("Status")

    def _status(ok: bool) -> str:
        return "[green]PASS[/green]" if ok else "[bold red]FAIL[/bold red]"

    metrics.add_row("Roots", f"{summary['n_roots']:,}", _status(checks["root_count_ok"]))
    metrics.add_row("Entries", f"{summary['n_entries']:,}", _status(checks["entry_count_ok"]))
    metrics.add_row("Needs review", f"{summary['n_needs_review']:,} ({summary['pct_needs_review']}%)", "")
    metrics.add_row("Average confidence", f"{summary['avg_confidence']:.4f}", "")
    metrics.add_row("Roots with 0 entries", str(summary["zero_entry_roots"]), _status(checks["zero_entry_roots_ok"]))
    metrics.add_row(
        "XRef resolution",
        f"{xref['resolved']:,}/{xref['total']:,} ({xref['resolution_rate_pct']}%)",
        _status(checks["xref_resolution_ok"]),
    )
    console.print(metrics)

    # Confidence distribution
    console.print("\n[bold]Confidence score distribution:[/bold]")
    conf_table = Table(show_header=True)
    conf_table.add_column("Threshold")
    conf_table.add_column("Entries at or above")
    conf_table.add_column("% of total")
    for threshold, count in report["confidence_distribution"].items():
        conf_table.add_row(threshold, str(count), _pct(count, summary["n_entries"]))
    console.print(conf_table)

    # Parse method breakdown
    console.print("\n[bold]Parse method breakdown:[/bold]")
    parse_table = Table(show_header=True)
    parse_table.add_column("Method")
    parse_table.add_column("Entries")
    parse_table.add_column("% of total")
    for method, count in report["parse_method_breakdown"].items():
        parse_table.add_row(method, str(count), _pct(count, summary["n_entries"]))
    console.print(parse_table)

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"\nReport written to {json_out}")

    if not summary["all_checks_passed"]:
        sys.exit(1)


if __name__ == "__main__":
    app()
