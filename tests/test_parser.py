"""
tests/test_parser.py
--------------------
Unit tests for the structural parser (src/pipeline/parser.py).

Tests are self-contained: they construct synthetic page_data dicts that
mimic what extract.py would produce, then assert on the parsed output.

No PDF or DB is required.
"""

from __future__ import annotations

import pytest

from hans_wehr.pipeline.parser import (
    ParsedEntry,
    RawSpan,
    _extract_plurals,
    _extract_pos,
    _extract_verb_form,
    _is_derived_span,
    _is_root_span,
    _roman_to_int,
    compute_confidence,
    parse_page,
    strip_diacritics,
)


# ---------------------------------------------------------------------------
# strip_diacritics
# ---------------------------------------------------------------------------

def test_strip_diacritics_removes_harakat():
    voweled = "كَتَبَ"
    assert strip_diacritics(voweled) == "كتب"


def test_strip_diacritics_noop_on_plain():
    plain = "كتب"
    assert strip_diacritics(plain) == plain


def test_strip_diacritics_noop_on_latin():
    assert strip_diacritics("hello") == "hello"


# ---------------------------------------------------------------------------
# _roman_to_int
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("roman,expected", [
    ("I", 1), ("II", 2), ("III", 3), ("IV", 4), ("V", 5),
    ("VI", 6), ("VII", 7), ("VIII", 8), ("IX", 9), ("X", 10),
])
def test_roman_to_int_valid(roman, expected):
    assert _roman_to_int(roman) == expected


def test_roman_to_int_invalid_raises():
    with pytest.raises(ValueError):
        _roman_to_int("ABC")


# ---------------------------------------------------------------------------
# _extract_verb_form
# ---------------------------------------------------------------------------

def test_extract_verb_form_present():
    assert _extract_verb_form("VIII. to listen") == "VIII"


def test_extract_verb_form_absent():
    assert _extract_verb_form("a book, letter") is None


def test_extract_verb_form_out_of_range():
    # XI doesn't exist in Hans Wehr
    assert _extract_verb_form("XI. some form") is None


# ---------------------------------------------------------------------------
# _extract_plurals
# ---------------------------------------------------------------------------

def test_extract_plurals_single():
    text = "book (pl. كُتُب)"
    plurals = _extract_plurals(text)
    assert "كتب" in plurals  # stored unvoweled


def test_extract_plurals_multiple():
    text = "word (pl. كَلِمَات, كَلِم)"
    plurals = _extract_plurals(text)
    assert len(plurals) == 2


def test_extract_plurals_absent():
    assert _extract_plurals("no plural here") == []


# ---------------------------------------------------------------------------
# _extract_pos
# ---------------------------------------------------------------------------

def _make_italic_span(text: str) -> RawSpan:
    return RawSpan(text=text, size=9.0, is_bold=False, is_italic=True, font="ArabicItalic", bbox=[0,0,0,0])


def test_extract_pos_noun():
    spans = [_make_italic_span("n. ")]
    assert _extract_pos(spans) == "noun"


def test_extract_pos_verb():
    spans = [_make_italic_span("v. ")]
    assert _extract_pos(spans) == "verb"


def test_extract_pos_missing():
    spans = [RawSpan(text="just definition", size=9.0, is_bold=False, is_italic=False, font="Regular", bbox=[0,0,0,0])]
    assert _extract_pos(spans) is None


# ---------------------------------------------------------------------------
# Font classification
# ---------------------------------------------------------------------------

def _make_span(text: str, size: float, is_bold: bool, is_italic: bool = False) -> RawSpan:
    return RawSpan(text=text, size=size, is_bold=is_bold, is_italic=is_italic, font="Test", bbox=[0,0,0,0])


def test_is_root_span_true():
    span = _make_span("كَتَبَ", 12.0, is_bold=True)
    assert _is_root_span(span) is True


def test_is_root_span_false_not_bold():
    span = _make_span("كَتَبَ", 12.0, is_bold=False)
    assert _is_root_span(span) is False


def test_is_root_span_false_too_small():
    span = _make_span("كَتَبَ", 8.0, is_bold=True)
    assert _is_root_span(span) is False


def test_is_derived_span_true():
    span = _make_span("كِتَابٌ", 9.5, is_bold=True)
    assert _is_derived_span(span) is True


def test_is_derived_span_false_too_large():
    span = _make_span("كِتَابٌ", 12.0, is_bold=True)
    # 12.0 >= ROOT_FONT_SIZE_MIN, so this is a root, not derived
    assert _is_derived_span(span) is False


# ---------------------------------------------------------------------------
# compute_confidence
# ---------------------------------------------------------------------------

def _base_entry(**kwargs) -> ParsedEntry:
    defaults = dict(
        root_arabic="كتب",
        root_unvoweled="كتب",
        root_translit="kataba",
        root_page=1,
        arabic="كِتَاب",
        arabic_unvoweled="كتاب",
        transliteration="kitāb",
        part_of_speech="noun",
        verb_form=None,
        plural_forms=[],
        definition="book",
        grammar_notes=None,
        page_number=1,
        confidence=1.0,
        needs_review=False,
        raw_text="كِتَاب kitāb n. book",
        warnings=[],
    )
    defaults.update(kwargs)
    return ParsedEntry(**defaults)


def test_confidence_perfect():
    entry = _base_entry()
    score = compute_confidence(entry)
    assert score == 1.0


def test_confidence_no_transliteration():
    entry = _base_entry(transliteration="")
    score = compute_confidence(entry)
    assert score < 1.0
    assert "no_transliteration" in entry.warnings


def test_confidence_no_pos():
    entry = _base_entry(part_of_speech=None)
    score = compute_confidence(entry)
    assert score < 1.0
    assert "no_pos_tag" in entry.warnings


def test_confidence_clamped_to_zero():
    entry = _base_entry(
        transliteration="",
        part_of_speech=None,
        arabic="",
        definition="",
    )
    score = compute_confidence(entry)
    assert score == 0.0


# ---------------------------------------------------------------------------
# parse_page integration test
# ---------------------------------------------------------------------------

def _make_page_data(spans: list[dict]) -> dict:
    """Construct a minimal page_data dict from a flat list of span dicts."""
    return {
        "page_number": 1,
        "width": 595.0,
        "height": 841.0,
        "blocks": [
            {
                "block_no": 0,
                "bbox": [0, 0, 595, 841],
                "lines": [
                    {
                        "line_no": i,
                        "bbox": [0, i * 20, 595, (i + 1) * 20],
                        "spans": [span],
                    }
                    for i, span in enumerate(spans)
                ],
            }
        ],
    }


def test_parse_page_single_entry():
    """A root span followed by a definition span should yield one entry."""
    page_data = _make_page_data([
        {"text": "كَتَبَ", "size": 12.0, "flags": 16, "is_bold": True, "is_italic": False,
         "font": "ArabicBold", "color": 0, "bbox": [10, 10, 100, 25]},
        {"text": "kataba", "size": 9.0, "flags": 0, "is_bold": False, "is_italic": False,
         "font": "Latin", "color": 0, "bbox": [105, 10, 200, 25]},
        {"text": "v. to write", "size": 9.0, "flags": 0, "is_bold": False, "is_italic": False,
         "font": "Regular", "color": 0, "bbox": [205, 10, 400, 25]},
    ])

    entries = list(parse_page(page_data))
    assert len(entries) >= 1
    first = entries[0]
    assert first["root_arabic"] == "كَتَبَ"
    assert first["page_number"] == 1


def test_parse_page_no_spans():
    page_data = {"page_number": 5, "width": 595.0, "height": 841.0, "blocks": []}
    entries = list(parse_page(page_data))
    assert entries == []


def test_parse_page_multiple_roots():
    """Two root spans should produce at least two entries (one per root)."""
    page_data = _make_page_data([
        {"text": "كَتَبَ", "size": 12.0, "flags": 16, "is_bold": True, "is_italic": False,
         "font": "ArabicBold", "color": 0, "bbox": [10, 0, 100, 20]},
        {"text": "to write", "size": 9.0, "flags": 0, "is_bold": False, "is_italic": False,
         "font": "Regular", "color": 0, "bbox": [105, 0, 300, 20]},
        {"text": "فَتَحَ", "size": 12.0, "flags": 16, "is_bold": True, "is_italic": False,
         "font": "ArabicBold", "color": 0, "bbox": [10, 30, 100, 50]},
        {"text": "to open", "size": 9.0, "flags": 0, "is_bold": False, "is_italic": False,
         "font": "Regular", "color": 0, "bbox": [105, 30, 300, 50]},
    ])
    entries = list(parse_page(page_data))
    roots = {e["root_arabic"] for e in entries}
    assert len(roots) >= 2
