"""Silver merge and a few gold stats on a tiny fake bronze cache.

Two uploaders log the same wipe ~3 s apart (clock drift); one of them also logs a kill.
"""

import dataclasses
import json
from datetime import UTC, date, datetime

import polars as pl
import pytest

from sga import bronze, gold, silver, suggest
from sga.season import ReportRef, Season

BOSS = 100
MELEE, PUDDLE = 1, 9
# 23:30 UTC = 00:30 in London (BST), so before the 06:00 cutoff: still the night of 1 Sep
START = int(datetime(2026, 9, 1, 23, 30, tzinfo=UTC).timestamp() * 1000)
ACTORS = [
    {"id": 1, "name": "Tank", "server": "S", "type": "Player", "subType": "Warrior"},
    {"id": 2, "name": "Heal", "server": "S", "type": "Player", "subType": "Priest"},
    {"id": 3, "name": "Dps", "server": "S", "type": "Player", "subType": "Mage"},
]
ABILITIES = [
    {"gameID": MELEE, "name": "Melee"},
    {"gameID": PUDDLE, "name": "Puddle"},
    {"gameID": 5512, "name": "Healthstone"},
]
PLAYERS = {
    "tanks": [{"id": 1, "specs": [{"spec": "Protection"}]}],
    "healers": [{"id": 2, "specs": [{"spec": "Holy"}]}],
    "dps": [{"id": 3, "specs": [{"spec": "Fire"}]}],
}


def fight(fid, start, end, kill, pct):
    return {
        "id": fid,
        "encounterID": BOSS,
        "name": "Boss",
        "difficulty": 4,
        "kill": kill,
        "size": 3,
        "startTime": start,
        "endTime": end,
        "bossPercentage": pct,
        "fightPercentage": pct,
        "wipeCalledTime": None,
    }


def tables(dps_damage):
    taken = [
        {"id": 1, "total": 500, "abilities": [{"guid": MELEE, "name": "Melee", "total": 500}]},
        {
            "id": 3,
            "total": 500,
            "abilities": [
                {"guid": MELEE, "name": "Melee", "total": 200},
                {"guid": PUDDLE, "name": "Puddle", "total": 300},
            ],
        },
    ]
    return {
        "damage_done": {"entries": [{"id": 3, "total": dps_damage}, {"id": 1, "total": 100}]},
        "healing": {"entries": [{"id": 2, "total": 800}]},
        "damage_taken": {"entries": taken},
        "interrupts": {"entries": []},
        "dispels": {"entries": []},
        "players": PLAYERS,
    }


def write_report(code, uploader, start, fights, fight_tables, deaths, consumables=()):
    rdir = bronze.report_dir(code)
    report = {
        "code": code,
        "startTime": start,
        "owner": {"id": 1, "name": uploader},
        "fights": fights,
        "masterData": {"actors": ACTORS, "abilities": ABILITIES},
    }
    files = {
        "report.json": report,
        "deaths.json": deaths,
        "casts.json": [],
        "consumables.json": list(consumables),
        "trash.json": {"fights": []},
    } | {f"fights/{fid}.json": t for fid, t in fight_tables.items()}
    for name, data in files.items():
        (rdir / name).parent.mkdir(parents=True, exist_ok=True)
        (rdir / name).write_text(json.dumps(data))


def death(target, ts, fid=1):
    return {"targetID": target, "timestamp": ts, "fight": fid, "killingAbilityGameID": PUDDLE}


@pytest.fixture
def season(tmp_path, monkeypatch):
    monkeypatch.setattr(bronze, "BRONZE", tmp_path)
    (tmp_path / "zones").mkdir()
    (tmp_path / "zones" / "1.json").write_text(json.dumps({"encounters": [{"id": BOSS}]}))
    # Ann: the wipe at +1 s and a kill; the Dps dies 30 s in, the healer after the wipe call
    write_report(
        "A",
        "Ann",
        START,
        [fight(1, 1_000, 61_000, False, 12.5), fight(2, 100_000, 200_000, True, 0)],
        {1: tables(1_000), 2: tables(5_000)},
        [death(3, 31_000), death(2, 59_000)],
        [{"sourceID": 3, "abilityGameID": 5512, "timestamp": 20_000, "fight": 1}],
    )
    # Bob's clock is 3 s ahead: the same wipe and the same Dps death, 3 s later
    write_report(
        "B",
        "Bob",
        START + 3_000,
        [fight(1, 1_000, 60_000, False, 10.0)],
        {1: tables(1_500)},
        [death(3, 31_000)],
    )
    return Season(
        slug="test",
        name="Test",
        zone_id=1,
        timezone="Europe/London",
        night_cutoff_hour=6,
        regular_threshold=0.1,
        guild={},
        uploaders=[],
        reports=[ReportRef("A", date(2026, 9, 1)), ReportRef("B", date(2026, 9, 2))],
        avoidable={BOSS: [PUDDLE]},
        avoidable_non_tank=[MELEE],
    )


@pytest.fixture
def merged(season):
    c = silver.parse_copies(season)
    return c | silver.merge(season, c)


def test_copies_merge_into_one_pull(merged):
    pulls = merged["pulls"].sort("start")
    assert pulls["copies"].to_list() == [2, 1]
    wipe = pulls.row(0, named=True)
    assert (wipe["kill"], wipe["boss_pct"], wipe["night"]) == (False, 10.0, date(2026, 9, 1))
    assert wipe["wipe_call_source"] == "fallback"


def test_player_metrics_take_the_max_never_the_sum(merged):
    dps = merged["player_pulls"].filter(pl.col("character") == "Dps-S").sort("pull_id")
    assert dps["damage_done"].to_list() == [1_500, 5_000]
    assert dps["healthstones"].to_list() == [1, 0]


def test_drifted_copies_of_a_death_count_once(merged):
    deaths = merged["deaths"].sort("ts")
    assert deaths["character"].to_list() == ["Dps-S", "Heal-S"]
    assert deaths["seen_in_copies"].to_list() == [2, 1]
    assert deaths["post_call"].to_list() == [False, True]


def test_report_dates_are_checked_against_their_nights(season, merged):
    # B is deliberately dated a day late
    assert silver.check_report_dates(season, merged) == [
        "report B is dated 2026-09-02 but covers 2026-09-01"
    ]


def test_roster_limits_regulars_but_not_totals(season, merged):
    rostered = dataclasses.replace(season, roster=["Dps", "Nobody"])
    c = gold.make_ctx(rostered, merged)
    assert c.regulars["player"].to_list() == ["Dps"]
    assert c.player_pulls["player"].n_unique() == 3
    assert gold.check_roster(c) == ["roster name Nobody matches no player"]


def test_avoidable_counts_melee_only_for_non_tanks(season, merged):
    out = gold.avoidable_damage(gold.make_ctx(season, merged))["avoidable_damage"]
    per_player = dict(out.select("player", "avoidable_damage").iter_rows())
    # Dps: Melee 200 + Puddle 300 on each of 2 pulls; the tank's melee doesn't count
    assert per_player == {"Dps": 1_000, "Tank": 0, "Heal": 0}


def test_meters_count_only_kills_in_the_role(season, merged):
    out = gold.meters(gold.make_ctx(season, merged))
    # The one kill: 100 s, Dps 5,000 damage, Heal 800 healing; the tank heals too but did none
    assert dict(out["dps_board"].select("player", "dps").iter_rows()) == {"Dps": 50}
    assert dict(out["hps_board"].select("player", "hps").iter_rows()) == {"Heal": 8, "Tank": 0}
    # Scores are against the median healer on the kill, here the only one
    assert dict(out["hps_board"].select("player", "score").iter_rows()) == {"Heal": 1, "Tank": 0}


def test_attendance_leaves_out_resets(season, merged):
    short = pl.when(pl.col("kill")).then(pl.col("duration_ms")).otherwise(10_000)
    reset = merged | {"pulls": merged["pulls"].with_columns(duration_ms=short)}
    out = gold.attendance(gold.make_ctx(season, reset))["attendance"]
    assert dict(out.select("player", "pulls_attended").iter_rows()) == {
        "Tank": 1,
        "Heal": 1,
        "Dps": 1,
    }
    assert out["resets"][0] == 1


def test_cockroach_skips_holy_priests(season, merged):
    out = gold.cockroach(gold.make_ctx(season, merged))["cockroach"]
    assert dict(out.select("player", "survived").iter_rows()) == {"Tank": 1, "Dps": 0}


def test_one_reports_fights_are_never_one_pull():
    # An accidental pull reset and re-pulled 18 s later, seen by A and by B
    fights = pl.DataFrame(
        {
            "report": ["A", "B", "A", "B"],
            "fight_id": [1, 1, 2, 2],
            "encounter_id": [BOSS] * 4,
            "difficulty": [4] * 4,
            "start": [0, 2_000, 18_000, 21_000],
        }
    )
    pulls = silver._assign_pulls(fights).sort("report", "fight_id")
    assert pulls["pull_id"].to_list() == [1, 2, 1, 2]


def test_wipe_call_is_on_the_pulls_timeline(season):
    c = silver.parse_copies(season)
    # Only Bob logged the call, 30 s in by his clock: before the Dps death at 31 s
    c["copy_fights"] = c["copy_fights"].with_columns(
        wipe_called=pl.when((pl.col("report") == "B") & (pl.col("fight_id") == 1))
        .then(START + 3_000 + 30_000)
        .otherwise(pl.col("wipe_called"))
    )
    deaths = silver.merge(season, c)["deaths"].sort("ts")
    assert deaths["post_call"].to_list() == [True, True]


def test_hit_share_leaves_tanks_out(merged):
    out = suggest.avoidable_candidates(merged, min_pulls=1)
    puddle = out.filter(pl.col("ability_id") == PUDDLE).row(0, named=True)
    # Only the Dps is hit, of the two non-tanks
    assert puddle["hit_share"] == 0.5
