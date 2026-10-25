"""slides.toml: the season's deck config is valid, and mistakes fail loudly."""

import polars as pl
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


def test_awards_cards_are_checked():
    card = {"data": "nope", "kicker": "k", "winner": "w", "stat": "s", "notes": "n"}
    sl = big_number_slide(layout="awards", cards=[card], title="t")
    del sl["number"]
    errors = slides.validate(config(sl))
    assert "slide x, cards 1: missing `punchline`" in errors
    assert "slide x, cards 1: unknown data `nope`" in errors


def test_every_tied_winner_is_named():
    # 1,040 and 1,004 both show as "1.0 k", so they tie; the runner-up is the next shown value
    df = pl.DataFrame({"player": ["A", "B", "C", "D"], "x": [1040, 1004, 900, 900]})
    out = slides.award(df, "x", slides.big)
    assert out == {"player": "A & B", "x": "1.0 k", "next": "C & D", "next_x": "900"}


def test_a_card_without_data_is_dropped():
    sl = {"id": "x", "layout": "awards", "cards": [{"data": "interrupts"}, {"data": "dispels"}]}
    stats = slides.StatTables(interrupts=pl.DataFrame({"player": ["A"], "interrupts": [3]}))
    logged = []
    kept, values = slides.entries(sl, stats, SEASON, {}, logged.append)
    assert kept == [{"data": "interrupts"}]
    assert values[0]["player"] == "A"
    assert logged[0].startswith("Skipping cards 2 of slide x: no data")


def test_a_broken_data_function_fails_instead_of_dropping_the_card():
    sl = {"id": "x", "layout": "awards", "cards": [{"data": "interrupts"}]}
    stats = slides.StatTables(interrupts=pl.DataFrame({"player": ["A"], "kicks": [3]}))
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        slides.entries(sl, stats, SEASON, {}, print)


def test_item_level_gaps_keep_their_sign():
    # +7.5 and -7.5 look alike without the sign; they mustn't tie
    df = pl.DataFrame(
        {"player": ["A", "B", "C"], "vs_raid": [7.5, 3.0, -7.5], "item_level": [1.0, 2.0, 3.0]}
    )
    out = slides.d_most_geared({"item_level": df}, SEASON)
    assert out["player"] == "A"
    assert out["vs_raid"] == "7.5 item levels above"
    assert out["next"] == "B"
    least = slides.d_least_geared({"item_level": df}, SEASON)
    assert (least["player"], least["vs_raid"]) == ("C", "7.5 item levels below")
    assert slides.gap(-0.04) == "level with"
