"""Helpers that suggest, but never apply, changes to season.toml."""

import time

import polars as pl

from sga import bronze
from sga.season import Season
from sga.wcl import WclClient

REPORT_LIST_FIELDS = """
  data { code title startTime owner { name } zone { id }
         fights(killType: Encounters) { difficulty } }
  has_more_pages
"""
DAY_MS = 86_400_000


def candidate_reports(client: WclClient, season: Season) -> pl.DataFrame:
    """Reports in the season's zone from the guild or the named uploaders that aren't listed."""
    listed = {r.code for r in season.reports}
    cached = [
        bronze.read(bronze.report_dir(r.code) / "report.json")
        for r in season.reports
        if (bronze.report_dir(r.code) / "report.json").exists()
    ]
    start = min((r["startTime"] for r in cached), default=0) - DAY_MS
    end = int(time.time() * 1000)
    owner_ids = {r["owner"]["name"]: r["owner"]["id"] for r in cached}

    sources = []
    if season.guild:
        g = season.guild
        selector = (
            f'guildName: "{g["name"]}", guildServerSlug: "{g["server"]}", '
            f'guildServerRegion: "{g["region"]}"'
        )
        sources.append((f"guild {g['name']}", selector))
    for name in season.uploaders:
        if name in owner_ids:
            sources.append((f"user {name}", f"userID: {owner_ids[name]}"))
        else:
            print(f"Skipping uploader {name}: no listed report of theirs to find their user ID")

    rows = {}
    for label, selector in sources:
        page = 1
        while True:
            data = client.query(
                f"reportData {{ reports({selector}, startTime: {start}, endTime: {end}, "
                f"page: {page}) {{ {REPORT_LIST_FIELDS} }} }}"
            )["reportData"]["reports"]
            for r in data["data"]:
                if r["code"] in listed or (r["zone"] or {}).get("id") != season.zone_id:
                    continue
                rows[r["code"]] = {
                    "code": r["code"],
                    "start": r["startTime"],
                    "owner": r["owner"]["name"],
                    "title": r["title"],
                    "boss_pulls": len(r["fights"] or []),
                    "found_via": label,
                }
            if not data["has_more_pages"]:
                break
            page += 1

    if not rows:
        return pl.DataFrame()
    return (
        pl.DataFrame(list(rows.values()))
        .with_columns(
            start=pl.from_epoch("start", time_unit="ms")
            .dt.replace_time_zone("UTC")
            .dt.convert_time_zone(season.timezone)
        )
        .sort("start")
    )


def avoidable_candidates(silver: dict[str, pl.DataFrame], min_pulls: int = 3) -> pl.DataFrame:
    """Enemy abilities that hit few players hard, rather than everyone evenly.

    For each encounter and ability: the share of present players hit on an average pull
    (`hit_share`), and how uneven the damage is among those hit (`unevenness`, the
    coefficient of variation). Low hit share with high unevenness suggests something
    people could have avoided; a person must approve each one.
    """
    pulls = silver["pulls"].select("pull_id", "encounter_id", "encounter")
    # Tanks' hits are left out below, so they're left out of the roster too
    present = silver["player_pulls"].join(
        silver["player_pulls"]
        .group_by("pull_id")
        .agg(roster=pl.col("role").ne_missing("tank").sum()),
        on="pull_id",
    )
    tanks = present.filter(pl.col("role") == "tank").select("pull_id", "character")
    hits = (
        silver["damage_taken"]
        .join(tanks, on=["pull_id", "character"], how="anti")
        .join(present.select("pull_id", "roster").unique(), on="pull_id")
        .join(pulls, on="pull_id")
    )
    per_pull = hits.group_by("encounter_id", "encounter", "ability_id", "ability", "pull_id").agg(
        hit=pl.len(),
        roster=pl.col("roster").first(),
        total=pl.col("amount").sum(),
        cv=(pl.col("amount").std() / pl.col("amount").mean()),
    )
    return (
        per_pull.group_by("encounter_id", "encounter", "ability_id", "ability")
        .agg(
            pulls=pl.len(),
            hit_share=(pl.col("hit") / pl.col("roster")).mean().round(2),
            unevenness=pl.col("cv").mean().round(2),
            avg_damage_per_pull=pl.col("total").mean().round(0),
        )
        .filter(pl.col("pulls") >= min_pulls)
        .with_columns(
            score=((1 - pl.col("hit_share")) * pl.col("unevenness").fill_null(0)).round(2)
        )
        .sort("encounter", "score", descending=[False, True])
    )
