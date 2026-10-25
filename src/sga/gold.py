"""Gold layer: season stats as small Polars functions over silver tables (docs/design.md).

Each stat function takes a Ctx and returns {name: DataFrame}. Register it in STATS.
`build` writes every stat to gold/stats/<name>.csv and all of them to gold/stats.json,
plus the pulls.csv and player_pulls.csv ledgers.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass

import polars as pl

from sga.season import Season

DIFFICULTY = {1: "LFR", 3: "Normal", 4: "Heroic", 5: "Mythic"}
TOP = 10
MELEE = 1  # Generic boss melee: not a mechanic, so left out of killing-blow stats
# Deaths this soon after the pull started were already happening before it
PRE_PULL_DEATH_MS = 1_000
# A wipe this short is most likely a reset: players who did nothing in it aren't logged
RESET_MS = 30_000
# Item level: a pull counts when at least this share of its players had theirs logged
ITEM_LEVEL_LOGGED_SHARE = 0.5
# The meters weigh each score as if the player had this many more kills at their role's average
SHRINK_KILLS = 10


@dataclass
class Ctx:
    season: Season
    pulls: pl.DataFrame
    player_pulls: pl.DataFrame
    deaths: pl.DataFrame
    casts: pl.DataFrame
    damage_taken: pl.DataFrame
    trash_nights: pl.DataFrame
    regulars: pl.DataFrame  # player, pulls_attended

    def is_regular(self, col: str = "player") -> pl.Expr:
        return pl.col(col).is_in(self.regulars["player"].implode())


def _player_expr(season: Season, col: str) -> pl.Expr:
    """Map a character column to its player (aliases), else the character's name."""
    mapping = {ch: player for player, chars in season.aliases.items() for ch in chars}
    # Character names have no hyphens, but server names can ("Azjol-Nerub")
    name_only = pl.col(col).str.replace(r"-.*$", "")
    return pl.col(col).replace_strict(mapping, default=name_only, return_dtype=pl.String)


def counted_pulls(pulls: pl.DataFrame) -> pl.DataFrame:
    """Pulls that aren't likely resets (see RESET_MS), as pull_id."""
    reset = ~pl.col("kill") & (pl.col("duration_ms") < RESET_MS)
    return pulls.filter(~reset).select("pull_id")


def make_ctx(season: Season, silver: dict[str, pl.DataFrame]) -> Ctx:
    excluded = set(season.excluded_characters)
    pulls = silver["pulls"].with_columns(
        boss=pl.col("encounter")
        + " "
        + pl.col("difficulty").replace_strict(DIFFICULTY, default="?", return_dtype=pl.String)
    )
    boss = pulls.select("pull_id", "boss", "night", "kill")

    def tag(df: pl.DataFrame, col: str) -> pl.DataFrame:
        return df.filter(~pl.col(col).is_in(excluded)).with_columns(
            player=_player_expr(season, col)
        )

    player_pulls = tag(silver["player_pulls"], "character").join(boss, on="pull_id")
    deaths = tag(silver["deaths"], "character").join(boss, on="pull_id")
    casts = (
        tag(silver["casts"], "source")
        .join(boss, on="pull_id")
        .with_columns(
            target_player=pl.when(pl.col("target").is_not_null()).then(
                _player_expr(season, "target")
            )
        )
    )
    damage_taken = tag(silver["damage_taken"], "character").join(boss, on="pull_id")
    trash_nights = tag(silver["trash_nights"], "character")

    # Counted the same way as the attendance stat
    counted = counted_pulls(pulls)
    attended = (
        player_pulls.join(counted, on="pull_id")
        .group_by("player")
        .agg(pulls_attended=pl.col("pull_id").n_unique())
    )
    regulars = attended.filter(
        pl.col("pulls_attended") >= season.regular_threshold * counted.height
    )
    if season.roster:
        regulars = regulars.filter(pl.col("player").is_in(season.roster))
    return Ctx(season, pulls, player_pulls, deaths, casts, damage_taken, trash_nights, regulars)


# Ties are broken by these columns, so the same data always gives the same order and winners
TIE_BREAK = ("player", "boss", "encounter", "ability", "night", "pull_id", "character")


def ordered(df: pl.DataFrame, *by: str, descending: bool | list[bool] = False) -> pl.DataFrame:
    """Sort by `by`, then by any tie-break columns the frame has."""
    desc = descending if isinstance(descending, list) else [descending] * len(by)
    ties = [c for c in TIE_BREAK if c in df.columns and c not in by]
    return df.sort([*by, *ties], descending=[*desc, *[False] * len(ties)])


def check_roster(c: Ctx) -> list[str]:
    """Roster names that match no player in the season's pulls, most likely typos."""
    players = set(c.player_pulls["player"])
    return [
        f"roster name {name} matches no player" for name in c.season.roster if name not in players
    ]


def _ms_to_h(col: str) -> pl.Expr:
    return (pl.col(col) / 3_600_000).round(2)


# --- Stats -----------------------------------------------------------------------------


def season_totals(c: Ctx):
    p = c.pulls
    wipes = p.filter(~pl.col("kill"))
    return {
        "season_totals": pl.DataFrame(
            {
                "nights": [p["night"].n_unique()],
                "pulls": [p.height],
                "kills": [p.height - wipes.height],
                "wipes": [wipes.height],
                "combat_hours": [round(p["duration_ms"].sum() / 3_600_000, 2)],
                "wipe_hours": [round(wipes["duration_ms"].sum() / 3_600_000, 2)],
                "deaths": [c.deaths.height],
                "players": [c.player_pulls["player"].n_unique()],
                "regulars": [c.regulars.height],
                "first_night": [p["night"].min()],
                "last_night": [p["night"].max()],
            }
        )
    }


def progression(c: Ctx):
    numbered = c.pulls.pipe(ordered, "start").with_columns(
        pull_no=pl.col("start").rank("ordinal").over("encounter_id", "difficulty")
    )
    by_boss = (
        numbered.group_by("encounter_id", "encounter", "difficulty", "boss")
        .agg(
            pulls=pl.len(),
            kills=pl.col("kill").sum(),
            first_kill_night=pl.col("night").filter(pl.col("kill")).min(),
            pulls_to_first_kill=pl.col("pull_no").filter(pl.col("kill")).min(),
            best_boss_pct=pl.col("boss_pct").min(),
            combat_hours=_ms_to_h("duration_ms").sum(),
            first_pull_night=pl.col("night").min(),
        )
        .pipe(ordered, "encounter_id", "difficulty")
    )
    # The slide shows each boss at the highest difficulty attempted
    highest = by_boss.filter(
        pl.col("difficulty") == pl.col("difficulty").max().over("encounter_id")
    ).pipe(ordered, "first_pull_night", "encounter")
    return {"progression_all": by_boss, "progression": highest}


def so_close(c: Ctx):
    return {
        "so_close": c.pulls.filter(~pl.col("kill"))
        .pipe(ordered, "boss_pct")
        .select("pull_id", "night", "boss", "boss_pct", "duration_ms")
        .head(TOP)
    }


def speedruns(c: Ctx):
    shortest = (
        c.pulls.filter(~pl.col("kill"))
        .pipe(ordered, "duration_ms")
        .select("pull_id", "night", "boss", "duration_ms")
        .head(TOP)
    )
    fastest_death = (
        c.deaths.filter(~pl.col("post_call") & (pl.col("since_pull_ms") >= PRE_PULL_DEATH_MS))
        .pipe(ordered, "since_pull_ms")
        .select("pull_id", "night", "boss", "player", "since_pull_ms", "ability")
        .head(TOP)
    )
    return {"shortest_wipes": shortest, "fastest_deaths": fastest_death}


def killing_blows(c: Ctx):
    top = (
        c.deaths.filter(pl.col("ability").is_not_null() & (pl.col("ability_id") != MELEE))
        .group_by("ability", "ability_id")
        .agg(deaths=pl.len(), bosses=pl.col("boss").unique().sort().str.join(", "))
        .pipe(ordered, "deaths", descending=True)
        .head(TOP)
    )
    personal = (
        c.deaths.filter(
            c.is_regular() & pl.col("ability").is_not_null() & (pl.col("ability_id") != MELEE)
        )
        .group_by("player", "ability")
        .agg(deaths=pl.len())
        .pipe(ordered, "deaths", descending=True)
        .group_by("player", maintain_order=True)
        .first()
        .pipe(ordered, "deaths", descending=True)
    )
    return {"top_killing_blows": top, "personal_nemesis": personal}


def toughest_night(c: Ctx):
    nights = (
        c.pulls.group_by("night")
        .agg(pulls=pl.len(), kills=pl.col("kill").sum())
        .join(c.deaths.group_by("night").agg(deaths=pl.len()), on="night", how="left")
        .with_columns(pl.col("deaths").fill_null(0))
        .pipe(ordered, "night")
    )
    return {"nights": nights}


def kill_cleanliness(c: Ctx):
    kills = (
        c.pulls.filter(pl.col("kill"))
        .select("pull_id", "night", "boss")
        .join(c.deaths.group_by("pull_id").agg(deaths=pl.len()), on="pull_id", how="left")
        .with_columns(pl.col("deaths").fill_null(0))
        .pipe(ordered, "deaths", "night", descending=[True, False])
    )
    return {"kills_by_deaths": kills}


def floor_awards(c: Ctx):
    firsts = (
        c.deaths.filter(~pl.col("post_call") & (pl.col("since_pull_ms") >= PRE_PULL_DEATH_MS))
        .pipe(ordered, "ts")
        .group_by("pull_id", maintain_order=True)
        .first()
        .group_by("player")
        .agg(first_deaths=pl.len())
    )
    deaths = c.deaths.group_by("player").agg(deaths=pl.len())
    table = (
        c.regulars.join(firsts, on="player", how="left")
        .join(deaths, on="player", how="left")
        .with_columns(pl.col("first_deaths", "deaths").fill_null(0))
        .with_columns(
            first_death_rate=(pl.col("first_deaths") / pl.col("pulls_attended")).round(3),
            deaths_per_pull=(pl.col("deaths") / pl.col("pulls_attended")).round(3),
        )
        .pipe(ordered, "first_deaths", descending=True)
    )
    return {"floor_awards": table}


def attendance(c: Ctx):
    """Pulls attended, leaving out likely resets (see RESET_MS)."""
    counted = counted_pulls(c.pulls)
    total = counted.height
    table = (
        c.player_pulls.join(counted, on="pull_id")
        .group_by("player")
        .agg(pulls_attended=pl.col("pull_id").n_unique())
        .with_columns(
            of_pulls=pl.lit(total),
            resets=pl.lit(c.pulls.height - total),
            share=(pl.col("pulls_attended") / total).round(3),
            regular=c.is_regular(),
        )
        .pipe(ordered, "pulls_attended", descending=True)
    )
    return {"attendance": table}


def damage_and_healing(c: Ctx):
    pp = c.player_pulls.join(c.pulls.select("pull_id", "duration_ms"), on="pull_id")
    totals = pl.DataFrame(
        {
            "damage_done": [pp["damage_done"].sum()],
            "healing": [pp["healing"].sum()],
            "damage_taken": [pp["damage_taken"].sum()],
        }
    )
    per_player = (
        pp.group_by("player")
        .agg(pl.col("damage_done", "healing", "damage_taken").sum())
        .pipe(ordered, "damage_done", descending=True)
    )
    non_tank_taken = (
        pp.filter((pl.col("role") != "tank") & c.is_regular())
        .group_by("player")
        .agg(damage_taken=pl.col("damage_taken").sum())
        .pipe(ordered, "damage_taken", descending=True)
        .head(TOP)
    )
    return {
        "damage_healing_totals": totals,
        "damage_healing_by_player": per_player,
        "most_damage_taken_non_tank": non_tank_taken,
    }


def _meter(c: Ctx, metric: str, roles: list[str], ref_role: str, per_second: str) -> pl.DataFrame:
    """Per regular and role: `metric` per second on each kill they played in that role, relative
    to the median `ref_role` player on that same kill, averaged and weighted by sample size.

    Comparing within a kill evens out bosses and gear growing over the season. Each score is
    pulled towards its role's average as if the player had SHRINK_KILLS more average kills, so
    a couple of good kills can't top the board. Wipes are left out: early deaths and the time
    after the wipe call drag everyone down.
    """
    rates = (
        c.player_pulls.filter(pl.col("kill"))
        .join(c.pulls.select("pull_id", "duration_ms"), on="pull_id")
        .with_columns(rate=pl.col(metric).fill_null(0) / pl.col("duration_ms") * 1000)
    )
    ref = (
        rates.filter(pl.col("role") == ref_role)
        .group_by("pull_id")
        .agg(ref=pl.col("rate").median())
        .filter(pl.col("ref") > 0)
    )
    kills = (
        rates.filter(c.is_regular() & pl.col("role").is_in(roles))
        .join(ref, on="pull_id")
        .with_columns(relative=pl.col("rate") / pl.col("ref"))
    )
    # Labelled by the spec played most in the role
    main_spec = (
        kills.group_by("player", "role", "class", "spec")
        .agg(pulls=pl.len())
        .pipe(ordered, "pulls", "class", "spec", descending=[True, False, False])
        .group_by("player", "role", maintain_order=True)
        .first()
        .drop("pulls")
    )
    role_avg = kills.group_by("role").agg(role_avg=pl.col("relative").mean())
    return (
        kills.group_by("player", "role")
        .agg(
            kills=pl.len(),
            relative=pl.col("relative").mean(),
            total=pl.col(metric).fill_null(0).sum(),
            seconds=pl.col("duration_ms").sum() / 1000,
        )
        .join(main_spec, on=["player", "role"])
        .join(role_avg, on="role")
        .with_columns(
            (pl.col("total") / pl.col("seconds")).round(0).alias(per_second),
            score=(
                (pl.col("kills") * pl.col("relative") + SHRINK_KILLS * pl.col("role_avg"))
                / (pl.col("kills") + SHRINK_KILLS)
            ).round(4),
            relative=pl.col("relative").round(4),
        )
        .select("player", "role", "class", "spec", "kills", per_second, "relative", "score")
        .pipe(ordered, "score", descending=True)
        .head(TOP)
    )


def meters(c: Ctx):
    return {
        "dps_board": _meter(c, "damage_done", ["dps"], "dps", "dps"),
        # Tanks heal too, so they can place on the healing board; everyone is measured
        # against the healers
        "hps_board": _meter(c, "healing", ["healer", "tank"], "healer", "hps"),
    }


def _leaderboard(c: Ctx, metric: str):
    return (
        c.player_pulls.filter(c.is_regular())
        .group_by("player")
        .agg(pl.col(metric).sum())
        .filter(pl.col(metric) > 0)
        .pipe(ordered, metric, descending=True)
    )


def interrupts(c: Ctx):
    return {"interrupts": _leaderboard(c, "interrupts")}


def dispels(c: Ctx):
    return {"dispels": _leaderboard(c, "dispels")}


def battle_rezzes(c: Ctx):
    rez = c.casts.filter(pl.col("kind") == "battle_rez")
    regulars = c.regulars["player"].implode()
    # A Soulstone Resurrection is cast by the player being rezzed
    soulstone = pl.col("ability") == "Soulstone Resurrection"
    given = (
        rez.filter(~soulstone & c.is_regular())
        .group_by("player")
        .agg(rezzes_given=pl.len())
        .pipe(ordered, "rezzes_given", descending=True)
    )
    received = (
        rez.with_columns(
            rezzed=pl.when(soulstone).then(pl.col("player")).otherwise(pl.col("target_player"))
        )
        .filter(pl.col("rezzed").is_in(regulars))
        .group_by("rezzed")
        .agg(rezzes_received=pl.len())
        .rename({"rezzed": "player"})
        .pipe(ordered, "rezzes_received", descending=True)
    )
    return {"rezzes_given": given, "rezzes_received": received}


def consumables(c: Ctx):
    table = (
        c.player_pulls.filter(c.is_regular())
        .group_by("player")
        .agg(pl.col("potions", "healthstones").sum(), pulls=pl.col("pull_id").n_unique())
        .pipe(ordered, "potions", "healthstones", descending=True)
    )
    return {"consumables": table}


def janitor(c: Ctx):
    """Damage done to raid trash, on the nights someone logged trash."""
    table = (
        c.trash_nights.filter(c.is_regular())
        .group_by("player")
        .agg(trash_damage=pl.col("damage_done").sum(), nights=pl.col("night").n_unique())
        .pipe(ordered, "trash_damage", descending=True)
    )
    coverage = pl.DataFrame(
        {
            "nights_with_trash": [c.trash_nights["night"].n_unique()],
            "nights": [c.pulls["night"].n_unique()],
        }
    )
    return {"janitor": table, "trash_coverage": coverage}


def cockroach(c: Ctx):
    """Wipes a regular was still alive at the end of (any death, even after the call, counts).

    Holy Priests are left out: Spirit of Redemption keeps them up past the wipe, so their
    deaths often aren't logged. So are likely resets, which everyone survives.
    """
    holy_priest = (pl.col("class") == "Priest") & (pl.col("spec") == "Holy")
    wipes = (
        c.player_pulls.join(counted_pulls(c.pulls), on="pull_id")
        .filter(~pl.col("kill") & c.is_regular() & ~holy_priest)
        .select("pull_id", "player")
    )
    died = c.deaths.select("pull_id", "player").unique()
    table = (
        wipes.join(died.with_columns(died=pl.lit(True)), on=["pull_id", "player"], how="left")
        .group_by("player")
        .agg(wipes=pl.len(), survived=pl.col("died").is_null().sum())
        .with_columns(survival_rate=(pl.col("survived") / pl.col("wipes")).round(3))
        .pipe(ordered, "survived", descending=True)
    )
    return {"cockroach": table}


def jack_of_all_trades(c: Ctx):
    table = (
        c.player_pulls.filter(c.is_regular() & pl.col("spec").is_not_null())
        .group_by("player")
        .agg(
            specs=pl.col("spec").n_unique(),
            roles=pl.col("role").drop_nulls().n_unique(),
            classes=pl.col("class").n_unique(),
            spec_list=(pl.col("spec") + " " + pl.col("class")).unique().sort().str.join(", "),
        )
        .pipe(ordered, "roles", "specs", descending=True)
    )
    return {"jack_of_all_trades": table}


def item_level(c: Ctx):
    """Per regular: equipped item level on each boss pull against the median of everyone in that
    pull, averaged. Comparing within a pull evens out gear growing over the season, so joining
    late doesn't make a player look geared. Levels that weren't logged are left out, and so
    are pulls where fewer than ITEM_LEVEL_LOGGED_SHARE of the players had theirs logged, and
    regulars with fewer such pulls than the regular threshold. vs_raid is rounded as the
    slide shows it, so ties on the slide are ties here."""
    logged = (
        c.player_pulls.with_columns(
            share=pl.col("item_level").is_not_null().mean().over("pull_id"),
        )
        .filter(pl.col("item_level").is_not_null() & (pl.col("share") >= ITEM_LEVEL_LOGGED_SHARE))
        .with_columns(raid=pl.col("item_level").median().over("pull_id"))
    )
    min_pulls = c.season.regular_threshold * logged["pull_id"].n_unique()
    table = (
        logged.filter(c.is_regular())
        .group_by("player")
        .agg(
            vs_raid=(pl.col("item_level") - pl.col("raid")).mean().round(1),
            item_level=pl.col("item_level").mean().round(1),
            pulls=pl.len(),
        )
        .filter(pl.col("pulls") >= min_pulls)
        .pipe(ordered, "vs_raid", descending=True)
    )
    return {"item_level": table}


def bloodlust_timing(c: Ctx):
    first_lust = (
        c.casts.filter(pl.col("kind") == "lust")
        .pipe(ordered, "ts")
        .group_by("pull_id", maintain_order=True)
        .first()
    )
    overall = first_lust.select(
        pulls_with_lust=pl.len(),
        avg_seconds=(pl.col("since_pull_ms").mean() / 1000).round(1),
        median_seconds=(pl.col("since_pull_ms").median() / 1000).round(1),
    )
    by_boss = (
        first_lust.group_by("boss")
        .agg(pulls=pl.len(), avg_seconds=(pl.col("since_pull_ms").mean() / 1000).round(1))
        .pipe(ordered, "avg_seconds")
    )
    by_caster = (
        first_lust.filter(c.is_regular())
        .group_by("player")
        .agg(lusts=pl.len())
        .pipe(ordered, "lusts", descending=True)
    )
    return {"lust_timing": overall, "lust_timing_by_boss": by_boss, "lust_casters": by_caster}


def avoidable_damage(c: Ctx):
    approved = [
        (enc, ability) for enc, abilities in c.season.avoidable.items() for ability in abilities
    ]
    non_tank = c.season.avoidable_non_tank
    if not approved and not non_tank:
        return {}
    keys = pl.DataFrame(
        approved, schema={"encounter_id": pl.Int64, "ability_id": pl.Int64}, orient="row"
    )
    roles = c.player_pulls.select("pull_id", "character", "role")
    hits = (
        c.damage_taken.join(c.pulls.select("pull_id", "encounter_id"), on="pull_id")
        .join(roles, on=["pull_id", "character"], how="left")
        .join(
            keys.with_columns(any_role=pl.lit(True)), on=["encounter_id", "ability_id"], how="left"
        )
        .filter(
            pl.col("any_role").fill_null(False)
            | (pl.col("ability_id").is_in(non_tank) & (pl.col("role") != "tank"))
        )
        .filter(c.is_regular())
    )
    # Per pull attended, so players who dodge everything still count their clean pulls
    main_role = c.player_pulls.group_by("player").agg(
        main_role=pl.col("role").mode().sort().first()
    )
    per_player = (
        hits.group_by("player")
        .agg(avoidable_damage=pl.col("amount").sum())
        .join(c.regulars, on="player", how="right")
        .join(main_role, on="player", how="left")
        .with_columns(pl.col("avoidable_damage").fill_null(0))
        .with_columns(per_pull=(pl.col("avoidable_damage") / pl.col("pulls_attended")).round(0))
        .select("player", "main_role", "avoidable_damage", "pulls_attended", "per_pull")
        .pipe(ordered, "per_pull", descending=True)
    )
    by_ability = (
        hits.group_by("boss", "ability_id", "ability")
        .agg(avoidable_damage=pl.col("amount").sum(), players_hit=pl.col("player").n_unique())
        .pipe(ordered, "avoidable_damage", descending=True)
    )
    return {"avoidable_damage": per_player, "avoidable_by_ability": by_ability}


STATS: list[Callable[[Ctx], dict[str, pl.DataFrame]]] = [
    season_totals,
    progression,
    so_close,
    speedruns,
    killing_blows,
    toughest_night,
    kill_cleanliness,
    floor_awards,
    attendance,
    damage_and_healing,
    meters,
    interrupts,
    dispels,
    battle_rezzes,
    consumables,
    bloodlust_timing,
    avoidable_damage,
    janitor,
    cockroach,
    jack_of_all_trades,
    item_level,
]


def compute(c: Ctx) -> dict[str, pl.DataFrame]:
    out: dict[str, pl.DataFrame] = {}
    for stat in STATS:
        out |= stat(c)
    return out


def build(season: Season, silver: dict[str, pl.DataFrame]) -> dict[str, pl.DataFrame]:
    c = make_ctx(season, silver)
    stats = compute(c)
    gold = season.gold_dir
    (gold / "stats").mkdir(parents=True, exist_ok=True)
    for name, df in stats.items():
        df.write_csv(gold / "stats" / f"{name}.csv")
    (gold / "stats.json").write_text(
        json.dumps({k: v.to_dicts() for k, v in stats.items()}, indent=1, default=str) + "\n"
    )
    # Ledgers: enough to trace any stat after bronze and silver are cleared
    c.pulls.write_csv(gold / "pulls.csv")
    c.player_pulls.write_csv(gold / "player_pulls.csv")
    return stats
