"""
tests/test_queries.py
---------------------
Unit tests for src/db/queries.py using in-memory SQLite.

All tests create a fresh in-memory DB, apply the schema, seed minimal data,
and verify query behaviour. No file system access required.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db.queries import (
    count_entries,
    count_needs_review,
    count_roots,
    count_unresolved_xrefs,
    get_entries_for_root,
    get_entry_by_id,
    get_root_by_arabic,
    get_root_by_transliteration,
    list_roots_by_letter,
    parse_plural_forms,
    strip_diacritics,
)

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def conn() -> sqlite3.Connection:
    """In-memory SQLite connection with schema applied and minimal seed data."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON;")

    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    db.executescript(schema)

    # Seed one root
    db.execute(
        "INSERT INTO roots (arabic, arabic_unvoweled, transliteration, page_number) VALUES (?, ?, ?, ?)",
        ("كَتَبَ", "كتب", "kataba", 42),
    )
    root_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Seed two entries under the root
    db.execute(
        """
        INSERT INTO entries
            (root_id, arabic, arabic_unvoweled, transliteration, part_of_speech,
             definition, page_number, confidence, needs_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (root_id, "كَتَبَ", "كتب", "kataba", "verb", "to write", 42, 0.98, 0),
    )
    db.execute(
        """
        INSERT INTO entries
            (root_id, arabic, arabic_unvoweled, transliteration, part_of_speech,
             definition, page_number, confidence, needs_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (root_id, "كِتَاب", "كتاب", "kitāb", "noun", "book", 42, 0.60, 1),
    )

    # Update entry_count
    db.execute("UPDATE roots SET entry_count = 2 WHERE id = ?", (root_id,))
    db.commit()

    yield db
    db.close()


# ---------------------------------------------------------------------------
# strip_diacritics
# ---------------------------------------------------------------------------

def test_strip_diacritics_basic():
    assert strip_diacritics("كَتَبَ") == "كتب"


def test_strip_diacritics_empty():
    assert strip_diacritics("") == ""


# ---------------------------------------------------------------------------
# Root queries
# ---------------------------------------------------------------------------

def test_get_root_by_arabic_voweled(conn):
    row = get_root_by_arabic(conn, "كَتَبَ")
    assert row is not None
    assert row["arabic_unvoweled"] == "كتب"


def test_get_root_by_arabic_unvoweled(conn):
    row = get_root_by_arabic(conn, "كتب")
    assert row is not None
    assert row["transliteration"] == "kataba"


def test_get_root_by_arabic_not_found(conn):
    row = get_root_by_arabic(conn, "فتح")
    assert row is None


def test_get_root_by_transliteration(conn):
    row = get_root_by_transliteration(conn, "kataba")
    assert row is not None
    assert row["page_number"] == 42


def test_get_root_by_transliteration_case_insensitive(conn):
    row = get_root_by_transliteration(conn, "KATABA")
    assert row is not None


def test_get_root_by_transliteration_not_found(conn):
    row = get_root_by_transliteration(conn, "fataḥa")
    assert row is None


def test_list_roots_by_letter_arabic(conn):
    rows = list_roots_by_letter(conn, "ك")
    assert len(rows) >= 1
    assert any(r["arabic_unvoweled"].startswith("ك") for r in rows)


def test_list_roots_by_letter_translit(conn):
    rows = list_roots_by_letter(conn, "k")
    assert len(rows) >= 1


# ---------------------------------------------------------------------------
# Entry queries
# ---------------------------------------------------------------------------

def test_get_entries_for_root(conn):
    root = get_root_by_arabic(conn, "كتب")
    entries = get_entries_for_root(conn, root["id"])
    assert len(entries) == 2


def test_get_entry_by_id(conn):
    root = get_root_by_arabic(conn, "كتب")
    all_entries = get_entries_for_root(conn, root["id"])
    entry_id = all_entries[0]["id"]
    row = get_entry_by_id(conn, entry_id)
    assert row is not None
    assert row["arabic_unvoweled"] == "كتب"


def test_get_entry_by_id_not_found(conn):
    row = get_entry_by_id(conn, 99999)
    assert row is None


# ---------------------------------------------------------------------------
# Count queries
# ---------------------------------------------------------------------------

def test_count_roots(conn):
    assert count_roots(conn) == 1


def test_count_entries(conn):
    assert count_entries(conn) == 2


def test_count_needs_review(conn):
    # One entry has needs_review = 1
    assert count_needs_review(conn) == 1


def test_count_unresolved_xrefs(conn):
    assert count_unresolved_xrefs(conn) == 0


# ---------------------------------------------------------------------------
# parse_plural_forms
# ---------------------------------------------------------------------------

def test_parse_plural_forms_valid():
    result = parse_plural_forms('["كتب", "أكتاب"]')
    assert result == ["كتب", "أكتاب"]


def test_parse_plural_forms_empty_json():
    assert parse_plural_forms("[]") == []


def test_parse_plural_forms_none():
    assert parse_plural_forms(None) == []


def test_parse_plural_forms_invalid_json():
    # Should return [] rather than raising
    assert parse_plural_forms("not json") == []
