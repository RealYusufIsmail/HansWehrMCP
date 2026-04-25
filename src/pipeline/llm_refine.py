"""
src/pipeline/llm_refine.py
--------------------------
Stage 3 (optional): send low-confidence parsed entries to the Anthropic API
and ask it to return corrected structured JSON.

This stage is NOT the primary parser — it is a cleanup pass. The structural
parse output and raw_text are always preserved in parse_metadata so the audit
trail is never lost.

Design:
  - Reads data/processed/entries.jsonl
  - Filters entries where needs_review == true
  - Batches them into API requests (up to BATCH_SIZE entries per call)
  - Before running, prints a cost estimate and asks for confirmation
  - Writes refined entries to data/processed/entries_refined.jsonl
  - Entries that were NOT refined are copied through unchanged

Usage:
  python -m src.pipeline.llm_refine --input data/processed/entries.jsonl \
      --out data/processed/entries_refined.jsonl
  python -m src.pipeline.llm_refine --dry-run  # cost estimate only, no API calls
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import anthropic
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

load_dotenv()

app = typer.Typer(help="LLM refinement pass for low-confidence parsed entries.")
console = Console()
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL = "claude-sonnet-4-20250514"
BATCH_SIZE = 10           # entries per API call
MAX_RETRIES = 5
INITIAL_RETRY_DELAY = 2.0  # seconds

# Approximate costs (USD per 1M tokens) — update if Anthropic changes pricing
INPUT_COST_PER_MTOK = 3.00
OUTPUT_COST_PER_MTOK = 15.00

# Conservative token estimates per entry for cost estimation
APPROX_INPUT_TOKENS_PER_ENTRY = 400
APPROX_OUTPUT_TOKENS_PER_ENTRY = 200

# Confidence threshold — entries below this are sent for refinement
CONFIDENCE_THRESHOLD = 0.75


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are an expert Arabic lexicographer specialising in the Hans Wehr Dictionary of Modern Written Arabic.
You will be given raw text extracted from the dictionary PDF along with a partially-parsed entry.
Your job is to return a corrected JSON object with accurate fields.

Rules:
1. Return ONLY valid JSON — no prose, no markdown code fences.
2. Preserve the exact Arabic text as it appears in the raw_text — do not transliterate yourself.
3. If a field is genuinely absent from the raw_text, return null for that field.
4. The 'definition' field must be the full English definition as printed — do not summarise.
5. Valid part_of_speech values: verb | noun | adjective | adverb | particle | phrase | proper_noun
6. Valid verb_form values: I | II | III | IV | V | VI | VII | VIII | IX | X | null
7. plural_forms must be a JSON array of Arabic strings (may be empty []).
8. Do not invent information not present in raw_text.
"""

ENTRY_SCHEMA = """\
{
  "arabic": "<Arabic word in script>",
  "arabic_unvoweled": "<same without diacritics>",
  "transliteration": "<Hans Wehr academic transliteration or null>",
  "part_of_speech": "<verb|noun|adjective|adverb|particle|phrase|proper_noun|null>",
  "verb_form": "<I–X or null>",
  "plural_forms": ["<Arabic plural>"],
  "definition": "<full English definition>",
  "grammar_notes": "<grammar note string or null>",
  "confidence": <float 0.0–1.0 — your confidence in this correction>,
  "correction_notes": "<brief explanation of what you changed>"
}
"""


def _build_user_message(batch: list[dict]) -> str:
    lines = ["Correct each of the following dictionary entries. Return a JSON array — one object per entry.\n"]
    lines.append(f"Each object must match this schema:\n{ENTRY_SCHEMA}\n")
    lines.append("Entries to correct:\n")
    for i, entry in enumerate(batch):
        lines.append(f"--- Entry {i + 1} ---")
        lines.append(f"raw_text: {entry.get('raw_text', '')}")
        lines.append(f"current_arabic: {entry.get('arabic', '')}")
        lines.append(f"current_definition: {entry.get('definition', '')}")
        lines.append(f"warnings: {entry.get('warnings', [])}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# API call with retry / rate-limit handling
# ---------------------------------------------------------------------------

def _call_api_with_retry(
    client: anthropic.Anthropic,
    user_message: str,
) -> tuple[str, int, int]:
    """Call the Anthropic API, retrying on rate-limit errors.

    Returns (response_text, input_tokens, output_tokens).
    """
    delay = INITIAL_RETRY_DELAY
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_message}],
            )
            text = response.content[0].text if response.content else ""
            usage = response.usage
            return text, usage.input_tokens, usage.output_tokens

        except anthropic.RateLimitError:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Rate limit hit, retrying in %.1f s (attempt %d/%d)", delay, attempt, MAX_RETRIES)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)

        except anthropic.APIStatusError as exc:
            if attempt == MAX_RETRIES or exc.status_code < 500:
                raise
            log.warning("API error %d, retrying in %.1f s", exc.status_code, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60.0)

    raise RuntimeError("Exhausted retries")  # unreachable, but satisfies type checker


def _parse_llm_response(raw_response: str, batch: list[dict]) -> list[dict]:
    """Parse the JSON array from the LLM response.

    If parsing fails, return the original entries unchanged with a warning added.
    """
    # Strip markdown code fences if the model included them
    text = raw_response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\n?", "", text)
        text = re.sub(r"\n?```$", "", text)

    try:
        corrections = json.loads(text)
    except json.JSONDecodeError as exc:
        log.error("LLM returned invalid JSON: %s — falling back to original entries", exc)
        for entry in batch:
            entry.setdefault("warnings", []).append("llm_invalid_json_response")
        return batch

    if not isinstance(corrections, list):
        corrections = [corrections]

    merged = []
    for i, entry in enumerate(batch):
        if i < len(corrections):
            correction = corrections[i]
            if isinstance(correction, dict):
                # Merge: LLM fields overwrite structural parse, but preserve audit fields
                updated = {**entry, **correction}
                updated["parse_method"] = "llm_refined"
                updated["llm_model"] = MODEL
                # Keep the original raw_text
                updated["raw_text"] = entry.get("raw_text", "")
                merged.append(updated)
            else:
                merged.append(entry)
        else:
            log.warning("LLM returned fewer corrections than batch size; keeping original for entry %d", i)
            merged.append(entry)

    return merged


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

def estimate_cost(n_entries: int) -> dict:
    n_batches = (n_entries + BATCH_SIZE - 1) // BATCH_SIZE
    total_input_tokens = n_entries * APPROX_INPUT_TOKENS_PER_ENTRY
    total_output_tokens = n_entries * APPROX_OUTPUT_TOKENS_PER_ENTRY
    input_cost = (total_input_tokens / 1_000_000) * INPUT_COST_PER_MTOK
    output_cost = (total_output_tokens / 1_000_000) * OUTPUT_COST_PER_MTOK
    return {
        "n_entries": n_entries,
        "n_batches": n_batches,
        "approx_input_tokens": total_input_tokens,
        "approx_output_tokens": total_output_tokens,
        "approx_cost_usd": round(input_cost + output_cost, 4),
    }


def _print_cost_table(cost: dict) -> None:
    table = Table(title="Cost Estimate", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="magenta")
    table.add_row("Entries to refine", str(cost["n_entries"]))
    table.add_row("API batches", str(cost["n_batches"]))
    table.add_row("Est. input tokens", f"{cost['approx_input_tokens']:,}")
    table.add_row("Est. output tokens", f"{cost['approx_output_tokens']:,}")
    table.add_row("Est. total cost (USD)", f"${cost['approx_cost_usd']:.4f}")
    console.print(table)


# ---------------------------------------------------------------------------
# Main refinement logic
# ---------------------------------------------------------------------------

import re  # noqa: E402 (imported again for _parse_llm_response — already imported above)


def refine_entries(
    input_path: Path,
    out_path: Path,
    dry_run: bool,
    threshold: float,
    limit: int | None,
) -> None:
    # Load all entries
    all_entries: list[dict] = []
    with input_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    all_entries.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    log.warning("Skipping malformed JSONL line: %s", exc)

    console.print(f"Loaded {len(all_entries)} total entries from {input_path}")

    # Split: needs refinement vs pass-through
    to_refine = [e for e in all_entries if e.get("needs_review") and e.get("confidence", 1.0) < threshold]
    pass_through = [e for e in all_entries if not (e.get("needs_review") and e.get("confidence", 1.0) < threshold)]

    if limit:
        to_refine = to_refine[:limit]

    console.print(f"{len(to_refine)} entries flagged for refinement, {len(pass_through)} pass-through.")

    cost = estimate_cost(len(to_refine))
    _print_cost_table(cost)

    if dry_run:
        console.print("[yellow]Dry-run mode:[/yellow] no API calls made.")
        return

    if len(to_refine) == 0:
        console.print("[green]No entries need refinement.[/green]")
        # Still write the pass-through entries
        _write_output(out_path, pass_through)
        return

    if not typer.confirm(f"Proceed with ~${cost['approx_cost_usd']:.4f} in API costs?"):
        console.print("Aborted.")
        raise typer.Exit(0)

    client = anthropic.Anthropic()

    refined: list[dict] = []
    total_input_tokens = 0
    total_output_tokens = 0

    batches = [to_refine[i:i + BATCH_SIZE] for i in range(0, len(to_refine), BATCH_SIZE)]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Refining entries", total=len(batches))

        for batch in batches:
            user_message = _build_user_message(batch)
            try:
                response_text, in_tok, out_tok = _call_api_with_retry(client, user_message)
                total_input_tokens += in_tok
                total_output_tokens += out_tok
                corrected_batch = _parse_llm_response(response_text, batch)
                refined.extend(corrected_batch)
            except Exception as exc:
                log.error("Batch failed: %s — keeping originals", exc)
                for entry in batch:
                    entry.setdefault("warnings", []).append(f"llm_batch_error:{exc}")
                refined.extend(batch)
            progress.advance(task)

    actual_cost = (
        (total_input_tokens / 1_000_000) * INPUT_COST_PER_MTOK
        + (total_output_tokens / 1_000_000) * OUTPUT_COST_PER_MTOK
    )
    console.print(
        f"[green]Done.[/green] Used {total_input_tokens:,} input + "
        f"{total_output_tokens:,} output tokens. "
        f"Actual cost: ~${actual_cost:.4f}"
    )

    # Merge refined + pass-through and write output
    # Preserve original page order by re-sorting on page_number then entry order
    all_output = pass_through + refined
    all_output.sort(key=lambda e: (e.get("page_number", 0), e.get("arabic", "")))
    _write_output(out_path, all_output)


def _write_output(out_path: Path, entries: list[dict]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    console.print(f"Wrote {len(entries)} entries to {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@app.command()
def main(
    input: Path = typer.Option(
        Path("data/processed/entries.jsonl"),
        "--input", "-i",
        help="JSONL file from parser.py.",
    ),
    out: Path = typer.Option(
        Path("data/processed/entries_refined.jsonl"),
        "--out", "-o",
        help="Output JSONL with refined entries merged in.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Print cost estimate and exit without making any API calls.",
    ),
    threshold: float = typer.Option(
        CONFIDENCE_THRESHOLD,
        "--threshold",
        help="Confidence score below which entries are sent for refinement.",
    ),
    limit: int | None = typer.Option(
        None,
        "--limit",
        help="Maximum number of entries to refine (for testing).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Send low-confidence entries to Claude for structured correction.

    Always estimates cost before making API calls and asks for confirmation.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if not input.exists():
        console.print(f"[bold red]Error:[/bold red] Input file not found: {input}")
        raise typer.Exit(2)

    refine_entries(input, out, dry_run, threshold, limit)


if __name__ == "__main__":
    app()
