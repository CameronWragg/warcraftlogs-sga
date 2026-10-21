"""slides.toml: the season's deck config is valid, and mistakes fail loudly."""

import pytest

from sga import season, slides

SEASON = season.load("venomous-abyss")


def big_number_slide(**overrides) -> dict:
    sl = {
        "id": "x",
        "layout": "big_number",
        "data": "none",
        "kicker": "k",
        "number": "{pulls}",
        "title": "t",
        "punchline": "p",
        "notes": "n",
    }
    return sl | overrides


def config(*slide_list) -> dict:
    return {"title": "{season}", "slides": list(slide_list)}


def test_season_config_is_valid():
    assert slides.validate(slides.load_config(SEASON)) == []


def test_structural_mistakes_are_listed():
    sl = big_number_slide(data="nope", subtitle="extra")
    del sl["punchline"]
    errors = slides.validate(config(sl, big_number_slide(layout="nope")) | {"sections": {}})
    assert "unknown top-level key `sections`" in errors
    assert any(e.startswith("slide x: unknown data `nope`") for e in errors)
    assert "slide x: missing `punchline` for layout big_number" in errors
    assert "slide x: unknown key `subtitle` for layout big_number" in errors
    assert "slide x: duplicate id" in errors
    assert "slide x: unknown layout `nope`" in errors


def test_unknown_placeholder_fails():
    with pytest.raises(
        slides.SlideConfigError, match=r"slide x, `title`: unknown placeholder 'nope'"
    ):
        slides.render(big_number_slide(title="{nope}"), {"pulls": "1"})


def test_case_variants_pick_by_case_and_fail_when_missing():
    sl = big_number_slide(title={"a": "A {pulls}", "b": "B"})
    assert "A 7" in slides.render(sl, {"pulls": "7", "case": "a"})
    with pytest.raises(slides.SlideConfigError, match="no variant for case `c`"):
        slides.render(sl, {"pulls": "7", "case": "c"})


def test_local_deck_holds_every_slide_once():
    pages = [slides.render(big_number_slide(id=f"s{i}"), {"pulls": "1"}) for i in range(3)]
    page = slides.local_deck("T", pages)
    assert page.count("<section ") == 3
    assert page.startswith("<!doctype html>")
    assert "<title>T</title>" in page
