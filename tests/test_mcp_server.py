"""
tests/test_mcp_server.py
------------------------
Unit tests for MCP tool dispatch logic in src/mcp/server.py.

These tests bypass the MCP transport layer and call the private dispatch
functions directly with an in-memory DB.

Tests verify:
  - Correct JSON response shapes
  - not_found responses for missing entries
  - Arabic + transliteration lookup paths
  - Error handling for bad input
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


# ---------------------------------------------------------------------------
# In-memory DB fixture (mirrors test_queries.py seed)
# ---------------------------------------------------------------------------

def _build_in_memory_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON;")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    db.executescript(schema)

    db.execute(
        "INSERT INTO roots (arabic, arabic_unvoweled, transliteration, page_number, entry_count) VALUES (?, ?, ?, ?, ?)",
        ("كَتَبَ", "كتب", "kataba", 42, 2),
    )
    root_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        """
        INSERT INTO entries (root_id, arabic, arabic_unvoweled, transliteration,
                             part_of_speech, definition, page_number, confidence, needs_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (root_id, "كَتَبَ", "كتب", "kataba", "verb", "to write; to correspond", 42, 0.98, 0),
    )
    entry_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        """
        INSERT INTO parse_metadata (entry_id, raw_text, parse_method, confidence, extraction_warnings)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entry_id, "كَتَبَ kataba v. to write", "structural", 0.98, "[]"),
    )
    db.execute(
        """
        INSERT INTO entries (root_id, arabic, arabic_unvoweled, transliteration,
                             part_of_speech, definition, page_number, confidence, needs_review)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (root_id, "كِتَاب", "كتاب", "kitāb", "noun", "book; letter", 42, 0.95, 0),
    )
    entry_id2 = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        """
        INSERT INTO parse_metadata (entry_id, raw_text, parse_method, confidence, extraction_warnings)
        VALUES (?, ?, ?, ?, ?)
        """,
        (entry_id2, "كِتَاب kitāb n. book", "structural", 0.95, "[]"),
    )
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Patch _db() to return our in-memory connection
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_db(monkeypatch):
    """Replace _db() with a factory that returns the in-memory test DB."""
    db = _build_in_memory_db()
    monkeypatch.setattr("src.mcp.server._db", lambda: db)
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Import dispatch functions after patching
# ---------------------------------------------------------------------------

from src.mcp.server import (  # noqa: E402
    _dispatch,
    _not_found_response,
)


# ---------------------------------------------------------------------------
# lookup_root tests
# ---------------------------------------------------------------------------

def test_lookup_root_by_arabic():
    result = json.loads(_dispatch("lookup_root", {"root": "كتب"}))
    assert "root" in result
    assert result["root"]["arabic_unvoweled"] == "كتب"
    assert result["total"] == 2


def test_lookup_root_by_transliteration():
    result = json.loads(_dispatch("lookup_root", {"root": "kataba"}))
    assert result["root"]["transliteration"] == "kataba"


def test_lookup_root_not_found():
    result = json.loads(_dispatch("lookup_root", {"root": "فتح"}))
    assert result["error"] == "not_found"


def test_lookup_root_missing_arg():
    result = json.loads(_dispatch("lookup_root", {}))
    assert "error" in result


# ---------------------------------------------------------------------------
# get_entry tests
# ---------------------------------------------------------------------------

def test_get_entry_valid(patch_db):
    # Get the first entry's ID
    row = patch_db.execute("SELECT id FROM entries LIMIT 1").fetchone()
    entry_id = row["id"]
    result = json.loads(_dispatch("get_entry", {"entry_id": entry_id}))
    assert result["id"] == entry_id
    assert "definition" in result
    assert "cross_references" in result


def test_get_entry_not_found():
    result = json.loads(_dispatch("get_entry", {"entry_id": 99999}))
    assert result["error"] == "not_found"


def test_get_entry_invalid_id():
    result = json.loads(_dispatch("get_entry", {"entry_id": "not_a_number"}))
    assert "error" in result


# ---------------------------------------------------------------------------
# list_roots tests
# ---------------------------------------------------------------------------

def test_list_roots_by_arabic_letter():
    result = json.loads(_dispatch("list_roots", {"letter": "ك"}))
    assert result["total"] >= 1
    assert all(r["arabic_unvoweled"].startswith("ك") for r in result["roots"])


def test_list_roots_by_latin_letter():
    result = json.loads(_dispatch("list_roots", {"letter": "k"}))
    assert result["total"] >= 1


def test_list_roots_no_match():
    result = json.loads(_dispatch("list_roots", {"letter": "ض"}))
    assert result["total"] == 0


def test_list_roots_missing_arg():
    result = json.loads(_dispatch("list_roots", {}))
    assert "error" in result


# ---------------------------------------------------------------------------
# Unknown tool
# ---------------------------------------------------------------------------

def test_unknown_tool():
    result = json.loads(_dispatch("nonexistent_tool", {}))
    assert "error" in result
    assert "Unknown tool" in result["error"]
