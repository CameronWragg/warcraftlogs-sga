"""Silver layer: per-copy tables parsed from bronze, and merged tables (docs/design.md).

Per-copy tables (one row per thing per report):
  copy_fights, copy_player_fights, copy_deaths, copy_casts, copy_damage_taken, copy_trash
Merged tables:
  copies         copy (report, fight) → pull_id
  pulls          one row per pull
  player_pulls   one row per character per pull, each metric the max across copies
  deaths         union of copies' deaths, deduped
  casts          union of copies' battle rez, lust, potion and healthstone casts, deduped
  damage_taken   per character per pull per enemy ability, max across copies
  trash_nights   damage done to raid trash per character per night: each uploader's reports
                 summed, then the max across uploaders
"""

import polars as pl

from sga import bronze
from sga.season import Season

SAME_PULL_MS = 20_000
SAME_DEATH_MS = 2_000
SAME_CAST_MS = 2_000
WIPE_CALL_FALLBACK_MS = 10_000

METRICS = [
    "damage_done",
    "healing",
    "damage_taken",
    "interrupts",
    "dispels",
]
# Counted per character per pull from the merged casts, not maxed per copy
CAST_COUNTS = {"battle_rez": "battle_rezzes", "potion": "potions", "healthstone": "healthstones"}


def _sum_details(table: dict) -> dict[int, int]:
    """Interrupts and dispels tables nest per-player counts under each enemy ability."""
    totals: dict[int, int] = {}
    for group in table.get("entries", []):
        for ability in group.get("entries", []):
            for d in ability.get("details", []):
                totals[d["id"]] = totals.get(d["id"], 0) + d["total"]
    return totals


def parse_copies(season: Season) -> dict[str, pl.DataFrame]:
    zone = bronze.read(bronze.BRONZE / "zones" / f"{season.zone_id}.json")
    zone_encounters = {e["id"] for e in zone["encounters"]}
    fights, players, deaths, casts, taken, trash = [], [], [], [], [], []

    for ref in season.reports:
        rdir = bronze.report_dir(ref.code)
        report = bronze.read(rdir / "report.json")
        r_start = report["startTime"]
        actors = {a["id"]: a for a in report["masterData"]["actors"]}
        abilities = {a["gameID"]: a["name"] for a in report["masterData"]["abilities"]}

        def character(actor_id, actors=actors):
            a = actors.get(actor_id)
            if a is None or a["type"] != "Player":
                return None
            return f"{a['name']}-{a['server']}"

        boss_fights = {f["id"]: f for f in report["fights"] if f["encounterID"] in zone_encounters}
        for f in boss_fights.values():
            fights.append(
                {
                    "report": ref.code,
                    "fight_id": f["id"],
                    "uploader": report["owner"]["name"],
                    "encounter_id": f["encounterID"],
                    "encounter": f["name"],
                    "difficulty": f["difficulty"],
                    "kill": bool(f["kill"]),
                    "boss_pct": f["bossPercentage"],
                    "fight_pct": f["fightPercentage"],
                    "start": r_start + f["startTime"],
                    "end": r_start + f["endTime"],
                    "wipe_called": r_start + f["wipeCalledTime"] if f["wipeCalledTime"] else None,
                }
            )
            tables = bronze.read(rdir / "fights" / f"{f['id']}.json")
            roles = {}
            pd = tables["players"]
            for role, key in (("tank", "tanks"), ("healer", "healers"), ("dps", "dps")):
                for p in pd.get(key, []):
                    roles[p["id"]] = (role, p)
            per_actor: dict[int, dict] = {}
            for metric in ("damage_done", "healing", "damage_taken"):
                for e in tables[metric].get("entries", []):
                    per_actor.setdefault(e["id"], {})[metric] = e["total"]
            for metric in ("interrupts", "dispels"):
                for actor_id, n in _sum_details(tables[metric]).items():
                    per_actor.setdefault(actor_id, {})[metric] = n
            for actor_id in roles:
                per_actor.setdefault(actor_id, {})
            for actor_id, m in per_actor.items():
                ch = character(actor_id)
                if ch is None:
                    continue
                role, p = roles.get(actor_id, (None, {}))
                spec = (p.get("specs") or [{}])[0].get("spec")
                players.append(
                    {
                        "report": ref.code,
                        "fight_id": f["id"],
                        "character": ch,
                        "class": actors[actor_id]["subType"],
                        "spec": spec,
                        "role": role,
                        # Equipped item level; WCL gives 0 when it wasn't logged
                        "item_level": p.get("maxItemLevel") or None,
                        **{k: m.get(k, 0) for k in METRICS},
                    }
                )
            for e in tables["damage_taken"].get("entries", []):
                ch = character(e["id"])
                if ch is None:
                    continue
                for a in e.get("abilities", []):
                    taken.append(
                        {
                            "report": ref.code,
                            "fight_id": f["id"],
                            "character": ch,
                            "ability_id": a["guid"],
                            "ability": a["name"],
                            "amount": a["total"],
                        }
                    )

        for d in bronze.read(rdir / "deaths.json"):
            ch = character(d["targetID"])
            if ch is None or d.get("fight") not in boss_fights:
                continue
            killer = actors.get(d.get("killerID"))
            ability_id = d.get("killingAbilityGameID")
            deaths.append(
                {
                    "report": ref.code,
                    "fight_id": d["fight"],
                    "character": ch,
                    "ts": r_start + d["timestamp"],
                    "killer": killer["name"] if killer else None,
                    "ability_id": ability_id,
                    "ability": abilities.get(ability_id) if ability_id else None,
                }
            )

        for c in bronze.read(rdir / "casts.json"):
            source = character(c["sourceID"])
            if source is None or c.get("fight") not in boss_fights:
                continue
            ability_id = c["abilityGameID"]
            casts.append(
                {
                    "report": ref.code,
                    "fight_id": c["fight"],
                    "kind": "battle_rez" if ability_id in bronze.BATTLE_REZ_ABILITIES else "lust",
                    "source": source,
                    "target": character(c.get("targetID", -1)),
                    "ability_id": ability_id,
                    "ability": bronze.CAST_ABILITIES[ability_id],
                    "ts": r_start + c["timestamp"],
                }
            )

        consumables = rdir / "consumables.json"
        for c in bronze.read(consumables) if consumables.exists() else []:
            source = character(c["sourceID"])
            if source is None or c.get("fight") not in boss_fights:
                continue
            name = abilities.get(c["abilityGameID"], "")
            casts.append(
                {
                    "report": ref.code,
                    "fight_id": c["fight"],
                    "kind": bronze.consumable_kind(name),
                    "source": source,
                    "target": None,
                    "ability_id": c["abilityGameID"],
                    "ability": name,
                    "ts": r_start + c["timestamp"],
                }
            )

        trash_path = rdir / "trash.json"
        t = bronze.read(trash_path) if trash_path.exists() else {"fights": []}
        if t["fights"]:
            first_trash = r_start + min(f["startTime"] for f in t["fights"])
            for e in t["damage_done"].get("entries", []):
                ch = character(e["id"])
                if ch is None:
                    continue
                trash.append(
                    {
                        "report": ref.code,
                        "uploader": report["owner"]["name"],
                        "character": ch,
                        "damage_done": e["total"],
                        "ts": first_trash,
                    }
                )

    return {
        "copy_fights": pl.DataFrame(fights, schema_overrides={"wipe_called": pl.Int64}),
        "copy_player_fights": pl.DataFrame(players, schema_overrides={"item_level": pl.Float64}),
        "copy_deaths": pl.DataFrame(deaths),
        "copy_casts": pl.DataFrame(casts, schema_overrides={"target": pl.String}),
        "copy_damage_taken": pl.DataFrame(taken),
        "copy_trash": pl.DataFrame(
            trash,
            schema={
                "report": pl.String,
                "uploader": pl.String,
                "character": pl.String,
                "damage_done": pl.Int64,
                "ts": pl.Int64,
            },
        ),
    }


def _assign_pulls(copy_fights: pl.DataFrame) -> pl.DataFrame:
    """Copies of the same encounter and difficulty starting within SAME_PULL_MS of the pull's
    first copy are one pull. Two fights from the same report are never the same pull."""
    ordered = copy_fights.sort("encounter_id", "difficulty", "start")
    keys, key, group, first_start, reports = [], 0, None, 0, set()
    for enc, diff, report, start in ordered.select(
        "encounter_id", "difficulty", "report", "start"
    ).iter_rows():
        if (enc, diff) != group or start - first_start > SAME_PULL_MS or report in reports:
            key += 1
            group, first_start, reports = (enc, diff), start, set()
        reports.add(report)
        keys.append(key)
    return (
        ordered.with_columns(pull_key=pl.Series(keys, dtype=pl.Int64))
        # Number pulls chronologically
        .with_columns(first_start=pl.col("start").min().over("pull_key"))
        .with_columns(pull_id=pl.col("first_start").rank("dense").cast(pl.Int64))
        .select("report", "fight_id", "pull_id")
    )


def _dedupe_events(df: pl.DataFrame, keys: list[str], window_ms: int) -> pl.DataFrame:
    """Rows with the same keys within window_ms of the previous one are the same event."""
    return df.sort(*keys, "ts").with_columns(
        event_key=((pl.col("ts").diff().over(keys).fill_null(window_ms + 1) > window_ms).cum_sum())
    )


def _night(ts_col: str, season: Season) -> pl.Expr:
    return (
        pl.from_epoch(pl.col(ts_col), time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone(season.timezone)
        .dt.offset_by(f"-{season.night_cutoff_hour}h")
        .dt.date()
    )


def merge(season: Season, c: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    copies = _assign_pulls(c["copy_fights"])
    # Uploaders' clocks drift by several seconds, so shift each copy's times onto the pull's
    # timeline (its earliest copy's clock, relative to the copy's own fight start) before
    # comparing or matching them
    cf = (
        c["copy_fights"]
        .join(copies, on=["report", "fight_id"])
        .with_columns(offset=pl.col("start").min().over("pull_id") - pl.col("start"))
        .with_columns(pl.col("start", "end", "wipe_called") + pl.col("offset"))
    )

    pulls = (
        cf.group_by("pull_id")
        .agg(
            pl.col("encounter_id").first(),
            pl.col("encounter").first(),
            pl.col("difficulty").first(),
            pl.col("kill").any(),
            pl.col("boss_pct").min(),
            pl.col("fight_pct").min(),
            pl.col("start").min(),
            pl.col("end").max(),
            pl.col("wipe_called").min(),
            pl.len().alias("copies"),
        )
        .with_columns(duration_ms=pl.col("end") - pl.col("start"))
        .with_columns(
            wipe_called=pl.when(pl.col("kill"))
            .then(None)
            .otherwise(pl.coalesce("wipe_called", pl.col("end") - WIPE_CALL_FALLBACK_MS)),
            wipe_call_source=pl.when(pl.col("kill"))
            .then(None)
            .when(pl.col("wipe_called").is_not_null())
            .then(pl.lit("logs"))
            .otherwise(pl.lit("fallback")),
            night=_night("start", season),
        )
        .sort("pull_id")
    )

    def with_pull(df: pl.DataFrame) -> pl.DataFrame:
        return df.join(copies, on=["report", "fight_id"])

    copy_offset = cf.select("report", "fight_id", "offset")

    def aligned(df: pl.DataFrame) -> pl.DataFrame:
        return (
            with_pull(df)
            .join(copy_offset, on=["report", "fight_id"])
            .with_columns(pl.col("ts") + pl.col("offset"))
            .drop("offset")
        )

    pp = with_pull(c["copy_player_fights"])
    player_pulls = (
        pp.group_by("pull_id", "character")
        .agg(
            pl.col("class").first(),
            pl.col("spec").drop_nulls().first(),
            pl.col("role").drop_nulls().first(),
            pl.col("item_level").max(),
            *[pl.col(m).max() for m in METRICS],
        )
        .join(pulls.select("pull_id", "duration_ms"), on="pull_id")
        .with_columns(
            dps=pl.col("damage_done") / (pl.col("duration_ms") / 1000),
            hps=pl.col("healing") / (pl.col("duration_ms") / 1000),
        )
        .drop("duration_ms")
    )

    deaths = (
        _dedupe_events(aligned(c["copy_deaths"]), ["pull_id", "character"], SAME_DEATH_MS)
        .group_by("pull_id", "character", "event_key")
        .agg(
            pl.col("ts").min(),
            pl.col("ability_id").drop_nulls().first(),
            pl.col("ability").drop_nulls().first(),
            pl.col("killer").drop_nulls().first(),
            pl.len().alias("seen_in_copies"),
        )
        .drop("event_key")
        .join(pulls.select("pull_id", "start", "wipe_called"), on="pull_id")
        .with_columns(
            since_pull_ms=pl.col("ts") - pl.col("start"),
            post_call=pl.col("wipe_called").is_not_null() & (pl.col("ts") > pl.col("wipe_called")),
        )
        .drop("start", "wipe_called")
        .sort("pull_id", "ts")
    )

    casts = (
        _dedupe_events(aligned(c["copy_casts"]), ["pull_id", "source", "ability_id"], SAME_CAST_MS)
        .group_by("pull_id", "source", "ability_id", "event_key")
        .agg(
            pl.col("kind").first(),
            pl.col("ability").first(),
            pl.col("target").drop_nulls().first(),
            pl.col("ts").min(),
        )
        .drop("event_key")
        .join(pulls.select("pull_id", "start"), on="pull_id")
        .with_columns(since_pull_ms=pl.col("ts") - pl.col("start"))
        .drop("start")
        .sort("pull_id", "ts")
    )

    cast_counts = (
        casts.filter(pl.col("kind").is_in(list(CAST_COUNTS)))
        .group_by("pull_id", "source")
        .agg(*[(pl.col("kind") == k).sum().alias(col) for k, col in CAST_COUNTS.items()])
        .rename({"source": "character"})
    )
    player_pulls = (
        player_pulls.join(cast_counts, on=["pull_id", "character"], how="left")
        .with_columns(pl.col(list(CAST_COUNTS.values())).fill_null(0))
        .sort("pull_id", "character")
    )

    damage_taken = (
        with_pull(c["copy_damage_taken"])
        .group_by("pull_id", "character", "ability_id")
        .agg(pl.col("ability").first(), pl.col("amount").max())
        .sort("pull_id", "character", "ability_id")
    )

    # A night's trash may be split over one uploader's reports but duplicated across uploaders
    trash_nights = (
        c["copy_trash"]
        .with_columns(night=_night("ts", season))
        .group_by("night", "uploader", "character")
        .agg(pl.col("damage_done").sum())
        .group_by("night", "character")
        .agg(pl.col("damage_done").max())
        .sort("night", "character")
    )

    return {
        "copies": copies,
        "pulls": pulls,
        "player_pulls": player_pulls,
        "deaths": deaths,
        "casts": casts,
        "damage_taken": damage_taken,
        "trash_nights": trash_nights,
    }


def check_report_dates(season: Season, tables: dict[str, pl.DataFrame]) -> list[str]:
    """Listed reports whose date isn't the night their pulls fall on, or with no pulls."""
    nights = (
        tables["copies"]
        .join(tables["pulls"].select("pull_id", "night"), on="pull_id")
        .group_by("report")
        .agg(pl.col("night").unique().sort())
    )
    found = dict(nights.iter_rows())
    problems = []
    for ref in season.reports:
        if ref.code not in found:
            problems.append(f"report {ref.code} has no pulls in the season's raid")
        elif found[ref.code] != [ref.date]:
            actual = ", ".join(str(n) for n in found[ref.code])
            problems.append(f"report {ref.code} is dated {ref.date} but covers {actual}")
    return problems


def build(season: Season) -> dict[str, pl.DataFrame]:
    tables = parse_copies(season)
    tables |= merge(season, tables)
    season.silver_dir.mkdir(parents=True, exist_ok=True)
    for name, df in tables.items():
        df.write_parquet(season.silver_dir / f"{name}.parquet")
    return tables


def load(season: Season) -> dict[str, pl.DataFrame]:
    return {p.stem: pl.read_parquet(p) for p in season.silver_dir.glob("*.parquet")}
