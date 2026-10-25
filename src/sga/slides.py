"""Slides: write <gold>/deck.html from gold stats and seasons/<slug>/slides.toml.

slides.toml declares every slide and all of its text (docs/design.md). This module supplies
the theme, the LAYOUTS, and per slide a DATA function returning values for {placeholders}.
Nothing has a default: a missing or unknown key, an unknown placeholder or a missing case
variant fails the build. Each slide is a 1920×1080 <section>; notes go in its <aside>.

  [[slides]]
  id = "killing-blow"     # the slide's id, unique in the deck
  layout = "big_number"   # see LAYOUTS for each layout's keys
  data = "killing_blow"   # a key of DATA; season_values are available to every slide
  enabled = true          # optional; false drops the slide
  kicker = "Top Killer"
  number = "{deaths}"
  title = "deaths to {ability}"
  punchline = "({bosses})"
  notes = "Abilities credited with the most killing blows. Next: {next}"

Text that depends on the numbers picks a variant: a data function returns a `case`, and any
text key may be a table keyed by case, e.g. punchline = { perfect = "...", top = "..." }.
Table rows work the same way with each row's own `case`.

A slide made of entries (stat_row items, awards cards) can give each entry its own `data`, so
one slide shows several awards; an entry whose stat is missing is dropped, not the slide.
Every slide that has a winner names all tied winners (see `winners`).
"""

import base64
import html
import re
import tomllib
from collections.abc import Callable
from datetime import date
from pathlib import Path

import polars as pl

from sga.gold import DIFFICULTY
from sga.season import Season

# Theme: World of Warcraft. UI gold and parchment over a blurred background picture, all text in
# Friz Quadrata. The font and the picture are supplied locally and git-ignored: drop them in
# ASSETS (any of the suffixes in MIME) and the deck embeds them; without them it falls back to
# a serif and a dark green gradient.
ASSETS = Path("assets")
FONT_FILE = "friz-quadrata"
BACKGROUND_FILE = "bg"
MIME = {
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff2": "font/woff2",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
}
INK = "#f5ecd7"
MUTED = "#d3c6a6"
GOLD = "#ffd100"
ORANGE = "#ff8000"
RULE = "rgba(255,255,255,.14)"
# The game's class colours, for the leaderboards
CLASS_COLOURS = {
    "DeathKnight": "#c41e3a",
    "DemonHunter": "#a330c9",
    "Druid": "#ff7c0a",
    "Evoker": "#33937f",
    "Hunter": "#aad372",
    "Mage": "#3fc7eb",
    "Monk": "#00ff98",
    "Paladin": "#f48cba",
    "Priest": "#ffffff",
    "Rogue": "#fff468",
    "Shaman": "#0070dd",
    "Warlock": "#8788ee",
    "Warrior": "#c69b6d",
}

Stats = dict[str, pl.DataFrame]
Values = dict[str, object]


class SlideConfigError(Exception):
    pass


# --- Formatting ------------------------------------------------------------------------


def esc(text) -> str:
    return html.escape(str(text), quote=False)


def n(value) -> str:
    return f"{value:,.0f}"


def big(value: float) -> str:
    for size, suffix in ((1e9, " b"), (1e6, " m"), (1e3, " k")):
        if abs(value) >= size:
            return f"{value / size:.1f}{suffix}"
    return n(value)


def mmss(ms: float) -> str:
    s = round(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


def day(d: date) -> str:
    return f"{d.day} {d:%b}"


def pct(value: float) -> str:
    return f"{value:.2f}%" if value < 1 else f"{value:.1f}%"


def row(stats: Stats, name: str, i: int = 0) -> dict:
    return stats[name].row(i, named=True)


def names(items) -> str:
    """'A', 'A & B', 'A, B & C'."""
    items = list(items)
    return " & ".join(filter(None, [", ".join(items[:-1]), *items[-1:]]))


def at(df: pl.DataFrame) -> str:
    """Where each pull of a table happened: 'Boss (30 Sep) & Boss (9 Sep)'."""
    return names(f"{r['boss']} ({day(r['night'])})" for r in df.iter_rows(named=True))


def spec_name(spec: str, cls: str) -> str:
    return f"{spec} {re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', cls)}"


# Ties: a slide names every winner, and its runner-up is the next distinct value. Both take a
# table already ordered by `by`, so the winners are its first rows. Values tie when they look
# the same on the slide, so tables are compared on a `shown` column (see `shown`).


def shown(df: pl.DataFrame, col: str, fmt: Callable) -> pl.DataFrame:
    """The table with `col` as the slide shows it, in a `shown` column."""
    return df.with_columns(shown=pl.Series([fmt(v) for v in df[col]], dtype=pl.String))


def winners(df: pl.DataFrame, *by: str) -> pl.DataFrame:
    """The rows tied with the first on `by`."""
    first = df.row(0, named=True)
    return df.filter(pl.all_horizontal([pl.col(c) == first[c] for c in by]))


def runners_up(df: pl.DataFrame, *by: str) -> pl.DataFrame:
    """The rows tied on the first value after the winners; empty if there is none."""
    rest = df.slice(winners(df, *by).height)
    return winners(rest, *by) if rest.height else rest


def places(df: pl.DataFrame, *by: str, n: int = 3) -> list[pl.DataFrame]:
    """The first `n` places of a table ordered by `by`; tied rows share a place."""
    out = []
    while df.height and len(out) < n:
        out.append(winners(df, *by))
        df = df.slice(out[-1].height)
    return out


def award(df: pl.DataFrame, col: str, fmt: Callable = n) -> Values:
    """Winners and runners-up of a per-player table ordered by `col`, as {player} and {col},
    {next} and {next_<col>}."""
    df = shown(df, col, fmt)
    w, r = winners(df, "shown"), runners_up(df, "shown")
    return {
        "player": names(w["player"]),
        col: w["shown"][0],
        "next": names(r["player"]),
        f"next_{col}": r["shown"][0] if r.height else "",
    }


def board(df: pl.DataFrame, per_second: str, size: int = 5) -> Values:
    """The top `size` rows of a meter by score as shown, plus anyone tied with the last."""
    df = shown(df, "score", lambda v: f"{v:.0%}").with_columns(
        pct=pl.col("score").mul(100).round(0)
    )
    top = df.filter(pl.col("pct") >= df["pct"][min(size, df.height) - 1])
    best = top["score"][0]
    return {
        "rows": [
            {
                "rank": 1 + (top["pct"] > r["pct"]).sum(),
                "player": r["player"],
                "spec": spec_name(r["spec"], r["class"]),
                "class": r["class"],
                "value": r["shown"],
                "share": r["score"] / best,
            }
            for r in top.iter_rows(named=True)
        ],
        "details": "; ".join(
            f"{r['player']} {big(r[per_second])} over {r['kills']} kills ({r['relative']:.0%})"
            for r in top.iter_rows(named=True)
        ),
    }


# --- Data: named values per slide ------------------------------------------------------


def season_values(st: Stats, s: Season) -> Values:
    t = row(st, "season_totals")
    return {
        "season": s.name,
        "nights": t["nights"],
        "pulls": n(t["pulls"]),
        "kills": n(t["kills"]),
        "wipes": n(t["wipes"]),
        "deaths": n(t["deaths"]),
        "regulars": t["regulars"],
        "combat_hours": f"{t['combat_hours']:.1f}",
        "wipe_hours": f"{t['wipe_hours']:.1f}",
        "combat_hours_exact": t["combat_hours"],
        "wipe_hours_exact": t["wipe_hours"],
        "wipe_share": f"{t['wipe_hours'] / t['combat_hours']:.0%}",
        "first_night": day(t["first_night"]),
        "last_night": day(t["last_night"]),
        "regular_threshold": f"{s.regular_threshold:.0%}",
    }


def d_none(st, s) -> Values:
    return {}


def d_progression(st, s) -> Values:
    p = st["progression"].sort(
        "first_kill_night", "first_pull_night", nulls_last=True, maintain_order=True
    )
    return {
        "rows": [
            {
                "case": "killed" if r["first_kill_night"] else "not_killed",
                "boss": r["encounter"],
                "difficulty": DIFFICULTY.get(r["difficulty"], "?"),
                "first_kill": day(r["first_kill_night"]) if r["first_kill_night"] else "",
                "pulls_to_kill": n(r["pulls_to_first_kill"]) if r["pulls_to_first_kill"] else "",
                "total_pulls": n(r["pulls"]),
                "best_pct": pct(r["best_boss_pct"]),
            }
            for r in p.iter_rows(named=True)
        ]
    }


def d_so_close(st, s) -> Values:
    sc = shown(st["so_close"], "boss_pct", pct)
    w, r = winners(sc, "shown"), runners_up(sc, "shown")
    return {
        "boss_pct": w["shown"][0],
        "where": at(w),
        "next_where": at(r),
        "next_boss_pct": r["shown"][0] if r.height else "",
        "next_closest": ", ".join(
            f"{x['boss']} {pct(x['boss_pct'])} ({day(x['night'])})"
            for x in sc.slice(w.height, 3).iter_rows(named=True)
        ),
    }


def d_speedrun(st, s) -> Values:
    sw = shown(st["shortest_wipes"], "duration_ms", mmss)
    w, r = winners(sw, "shown"), runners_up(sw, "shown")
    return {
        "duration": w["shown"][0],
        "seconds": f"{w['duration_ms'][0] / 1000:.1f}",
        "where": at(w),
        "pull_id": names(str(p) for p in w["pull_id"]),
        "next_where": at(r),
        "next_duration": r["shown"][0] if r.height else "",
    }


def d_fastest_death(st, s) -> Values:
    fd = shown(st["fastest_deaths"], "since_pull_ms", lambda ms: f"{ms / 1000:.1f}")
    w, r = winners(fd, "shown"), runners_up(fd, "shown")
    return {
        "player": names(w["player"].unique(maintain_order=True)),
        "seconds": w["shown"][0],
        "how": names(
            f"{x['ability'] or 'the floor'} on {x['boss']}" for x in w.iter_rows(named=True)
        ),
        "detail": "; ".join(
            f"{x['player']}: pull {x['pull_id']} on {day(x['night'])}"
            for x in w.iter_rows(named=True)
        ),
        "next": names(r["player"]),
        "next_seconds": r["shown"][0] if r.height else "",
    }


def d_killing_blow(st, s) -> Values:
    t = st["top_killing_blows"]
    w = winners(t, "deaths")
    return {
        "deaths": n(w["deaths"][0]),
        "ability": names(w["ability"]),
        "bosses": ", ".join(sorted({b for x in w["bosses"] for b in x.split(", ")})),
        "next": ", ".join(
            f"{x['ability']} {x['deaths']}" for x in t.slice(w.height, 3).iter_rows(named=True)
        ),
    }


def d_toughest_night(st, s) -> Values:
    nights = st["nights"]
    w = winners(nights.sort("deaths", descending=True, maintain_order=True), "deaths")
    top = w.row(0, named=True)
    return {
        "case": "one" if w.height == 1 else "tied",
        "deaths": n(top["deaths"]),
        "night": names(day(d) for d in w["night"]),
        "pulls": top["pulls"],
        "kills": top["kills"],
        "zero_kill_nights": nights.filter(pl.col("kills") == 0).height,
    }


def d_cleanest_kills(st, s) -> Values:
    k = st["kills_by_deaths"]
    return {"clean": k.filter(pl.col("deaths") == 0).height, "kills": k.height}


def d_ugliest_kill(st, s) -> Values:
    w = winners(st["kills_by_deaths"], "deaths")
    return {
        "deaths": n(w["deaths"][0]),
        "where": at(w),
        "pull_id": names(str(p) for p in w["pull_id"]),
    }


def d_floor_award(st, s) -> Values:
    f = st["floor_awards"]
    by_rate = f.sort("first_death_rate", descending=True, maintain_order=True)
    rate = winners(shown(by_rate, "first_death_rate", lambda r: f"{r:.0%}"), "shown")
    return award(f, "first_deaths") | {
        "rate_player": names(rate["player"]),
        "rate": rate["shown"][0],
    }


def d_attendance(st, s) -> Values:
    a = award(st["attendance"], "pulls_attended")
    top = row(st, "attendance")
    total = top["of_pulls"]
    return {
        "resets": top["resets"],
        "case": "perfect" if a["pulls_attended"] == n(total) else "top",
        "player": a["player"],
        "attended": a["pulls_attended"],
        "total": n(total),
        "next": a["next"],
        "next_attended": a["next_pulls_attended"],
    }


def d_damage_healing(st, s) -> Values:
    t = row(st, "damage_healing_totals")
    return {
        "damage": big(t["damage_done"]),
        "healing": big(t["healing"]),
        "damage_exact": n(t["damage_done"]),
        "healing_exact": n(t["healing"]),
    }


def d_dps_board(st, s) -> Values:
    return board(st["dps_board"], "dps")


def d_hps_board(st, s) -> Values:
    return board(st["hps_board"], "hps")


def d_damage_sponge(st, s) -> Values:
    return award(st["most_damage_taken_non_tank"], "damage_taken", big)


def d_avoidable(st, s) -> Values:
    a = st["avoidable_damage"]
    # Tanks are exempt from most of the list, so the cleanest feet go to a non-tank
    non_tanks = a.filter(pl.col("main_role") != "tank").sort("per_pull", maintain_order=True)
    clean = winners(shown(non_tanks, "per_pull", big), "shown")
    return award(a, "per_pull", big) | {
        "clean_player": names(clean["player"]),
        "clean_per_pull": clean["shown"][0],
        "sources": ", ".join(
            f"{x['ability']} ({x['boss']}) {big(x['avoidable_damage'])}"
            for x in st["avoidable_by_ability"].head(8).iter_rows(named=True)
        ),
    }


def d_cookie_monster(st, s) -> Values:
    t = st["consumables"].sort("healthstones", descending=True, maintain_order=True)
    top = t.row(0, named=True)
    return award(t, "healthstones") | {
        "per_pull": f"{top['healthstones'] / top['pulls']:.1f}",
        "pulls": n(top["pulls"]),
    }


def d_janitor(st, s) -> Values:
    cov = row(st, "trash_coverage")
    return award(st["janitor"], "trash_damage", big) | {
        "nights_with_trash": cov["nights_with_trash"],
        "nights": cov["nights"],
    }


def d_cockroach(st, s) -> Values:
    top = row(st, "cockroach")
    return award(st["cockroach"], "survived") | {
        "wipes": top["wipes"],
        "rate": f"{top['survival_rate']:.0%}",
    }


def d_jack_of_all_trades(st, s) -> Values:
    j = st["jack_of_all_trades"]
    podium = places(j, "roles", "specs")
    return {
        "rows": [
            {
                "place": i,
                "player": names(p["player"]),
                "roles": p["roles"][0],
                "specs": p["specs"][0],
            }
            for i, p in enumerate(podium, 1)
        ],
        "one_spec": j.filter(pl.col("specs") == 1).height,
        "spec_list": "; ".join(
            f"{x['player']}: {x['spec_list']}" for p in podium for x in p.iter_rows(named=True)
        ),
    }


def _gear(t: pl.DataFrame) -> Values:
    """An item level award on a table ordered winner first. The gap to the raid is shown
    without its sign: the slide says above or below."""
    t = shown(t, "vs_raid", lambda v: f"{abs(v):.1f}")
    w, r = winners(t, "shown"), runners_up(t, "shown")
    return {
        "player": names(w["player"]),
        "vs_raid": w["shown"][0],
        "item_level": names(f"{v:.1f}" for v in w["item_level"]),
        "next": names(r["player"]),
        "next_vs_raid": r["shown"][0] if r.height else "",
    }


def d_most_geared(st, s) -> Values:
    return _gear(st["item_level"])


def d_least_geared(st, s) -> Values:
    return _gear(st["item_level"].sort("vs_raid", maintain_order=True))


def d_interrupts(st, s) -> Values:
    return award(st["interrupts"], "interrupts")


def d_dispels(st, s) -> Values:
    return award(st["dispels"], "dispels")


def d_rezzes_given(st, s) -> Values:
    return award(st["rezzes_given"], "rezzes_given")


def d_rezzes_received(st, s) -> Values:
    return award(st["rezzes_received"], "rezzes_received")


def d_consumables(st, s) -> Values:
    t = st["consumables"].sort("potions", descending=True, maintain_order=True)
    never = t.filter((pl.col("potions") == 0) & (pl.col("healthstones") == 0))["player"]
    return award(t, "potions") | {
        "case": "some_never" if never.len() else "all_pressed",
        "never": names(never),
    }


def d_lustiest(st, s) -> Values:
    t = row(st, "lust_timing")
    return award(st["lust_casters"], "lusts") | {
        "pulls_with_lust": t["pulls_with_lust"],
        "median": f"{t['median_seconds']:.0f}",
        "mean": f"{t['avg_seconds']:.0f}",
    }


DATA: dict[str, Callable[[Stats, Season], Values]] = {
    "none": d_none,
    "progression": d_progression,
    "so_close": d_so_close,
    "speedrun": d_speedrun,
    "fastest_death": d_fastest_death,
    "killing_blow": d_killing_blow,
    "toughest_night": d_toughest_night,
    "cleanest_kills": d_cleanest_kills,
    "ugliest_kill": d_ugliest_kill,
    "floor_award": d_floor_award,
    "attendance": d_attendance,
    "damage_healing": d_damage_healing,
    "dps_board": d_dps_board,
    "hps_board": d_hps_board,
    "damage_sponge": d_damage_sponge,
    "avoidable": d_avoidable,
    "cookie_monster": d_cookie_monster,
    "janitor": d_janitor,
    "cockroach": d_cockroach,
    "jack_of_all_trades": d_jack_of_all_trades,
    "interrupts": d_interrupts,
    "dispels": d_dispels,
    "rezzes_given": d_rezzes_given,
    "rezzes_received": d_rezzes_received,
    "consumables": d_consumables,
    "lustiest": d_lustiest,
    "most_geared": d_most_geared,
    "least_geared": d_least_geared,
}
# What a data function raises when its stat is missing, e.g. no avoidable abilities approved
NO_DATA = (KeyError, IndexError, pl.exceptions.PolarsError)


# --- Layouts ---------------------------------------------------------------------------
# Each takes the slide id and its resolved config (all text already filled in). Every big
# number shrinks to fit its line when the deck loads (LOCAL_JS), then wraps.


def _kicker(text: str) -> str:
    return f'<h3 class="kicker">{esc(text)}</h3>' if text else ""


def _p(cls: str, text: str) -> str:
    """A line of text, left out when empty so it takes no space."""
    return f'<p class="{cls}">{esc(text)}</p>' if text else ""


# GitHub's mark (Octicons mark-github), for links to a repository
GITHUB_MARK = (
    '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M8 0c4.42 0 8 '
    "3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 "
    "0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02"
    "-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82"
    "-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95"
    "-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01"
    ".53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7"
    '.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z"/></svg>'
)


def _github(url: str) -> str:
    """A link to a repository at the bottom centre of the slide, left out when empty."""
    if not url:
        return ""
    label = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    return f'<a class="github" href="{html.escape(url)}">{GITHUB_MARK}{esc(label)}</a>'


def cover(sid: str, c: dict) -> str:
    """A title page; `github` optionally links a repository at the bottom."""
    return f"""<section id="{sid}" class="cover">
{_kicker(c["kicker"])}
<h1>{esc(c["title"])}</h1>
{_p("sub", c["subtitle"])}
{_github(c.get("github", ""))}
<aside>{esc(c["notes"])}</aside>
</section>"""


def big_number(sid: str, c: dict) -> str:
    """A guild or boss stat: the number is the headline."""
    return f"""<section id="{sid}" class="big-number">
{_kicker(c["kicker"])}
<h1 class="number">{esc(c["number"])}</h1>
<p class="title">{esc(c["title"])}</p>
{_p("punch", c["punchline"])}
<aside>{esc(c["notes"])}</aside>
</section>"""


def big_name(sid: str, c: dict) -> str:
    """A player award: the winner is the headline, the number goes in the title."""
    return f"""<section id="{sid}" class="big-name">
{_kicker(c["kicker"])}
<h1 class="number">{esc(c["winner"])}</h1>
<p class="title">{esc(c["title"])}</p>
{_p("punch", c["punchline"])}
<aside>{esc(c["notes"])}</aside>
</section>"""


def stat_row(sid: str, c: dict) -> str:
    """Up to four big values side by side on cards, each with an optional kicker."""
    cells = "".join(
        f'<div class="card">{_kicker(i.get("kicker", ""))}<p class="number">{esc(i["value"])}</p>'
        f'<p class="label">{esc(i["label"])}</p></div>'
        for i in c["items"]
    )
    return f"""<section id="{sid}" class="stat-row">
{_kicker(c["kicker"])}
<h2>{esc(c["title"])}</h2>
<div class="cells">{cells}</div>
{_p("punch", c["punchline"])}
<aside>{esc(c["notes"])}</aside>
</section>"""


def awards(sid: str, c: dict) -> str:
    """Up to four award cards; four go two by two. Each card's notes join the slide's."""
    cards = "".join(
        f'<div class="card">{_kicker(k["kicker"])}<p class="number">{esc(k["winner"])}</p>'
        f'<p class="stat">{esc(k["stat"])}</p>{_p("punch", k["punchline"])}</div>'
        for k in c["cards"]
    )
    quad = len(c["cards"]) == 4
    cols = 2 if quad else len(c["cards"])
    notes = " ".join(
        filter(
            None, [c["notes"], *(f"{k['kicker']}: {k['notes']}" for k in c["cards"] if k["notes"])]
        )
    )
    return f"""<section id="{sid}" class="awards">
{_kicker(c["kicker"])}
<h2>{esc(c["title"])}</h2>
<div class="cards{" quad" * quad}" style="grid-template-columns:repeat({cols}, 1fr)">{cards}</div>
{_p("punch", c["punchline"])}
<aside>{esc(notes)}</aside>
</section>"""


def table(sid: str, c: dict) -> str:
    th = "".join(f"<th>{esc(h)}</th>" for h in c["header"])
    trs = "".join(
        "<tr>" + "".join(f"<td>{esc(cell)}</td>" for cell in r) + "</tr>" for r in c["rows"]
    )
    return f"""<section id="{sid}">
{_kicker(c["kicker"])}
<h2>{esc(c["title"])}</h2>
<table><tr>{th}</tr>{trs}</table>
{_p("punch", c["punchline"])}
<aside>{esc(c["notes"])}</aside>
</section>"""


def leaderboard(sid: str, c: dict) -> str:
    """Ranked rows of [name, detail, value], each with a bar in the player's class colour."""
    lis = "".join(
        f'<li><span class="rank">{d["rank"]}</span>'
        f'<span class="who" style="color:{CLASS_COLOURS.get(d["class"], INK)}">{esc(name)}</span>'
        f'<span class="detail">{esc(detail)}</span>'
        f'<span class="bar"><i style="width:{d["share"]:.1%};'
        f'background:{CLASS_COLOURS.get(d["class"], GOLD)}"></i></span>'
        f'<span class="value">{esc(value)}</span></li>'
        for (name, detail, value), d in zip(c["rows"], c["data_rows"], strict=True)
    )
    return f"""<section id="{sid}" class="leaderboard">
{_kicker(c["kicker"])}
<h2>{esc(c["title"])}</h2>
<ol>{lis}</ol>
{_p("punch", c["punchline"])}
<aside>{esc(c["notes"])}</aside>
</section>"""


def podium(sid: str, c: dict) -> str:
    """The top three places as rows of [name, detail], first in the middle; names take their
    place's medal colour."""
    order = {2: 0, 1: 1, 3: 2}
    steps = "".join(
        f'<div class="step p{d["place"]}" style="order:{order.get(d["place"], 3)}">'
        f'<p class="number">{esc(name)}</p><p class="label">{esc(detail)}</p>'
        '<div class="plinth"></div></div>'
        for (name, detail), d in zip(c["rows"], c["data_rows"], strict=True)
    )
    return f"""<section id="{sid}" class="podium">
{_kicker(c["kicker"])}
<h2>{esc(c["title"])}</h2>
<div class="steps">{steps}</div>
{_p("punch", c["punchline"])}
<aside>{esc(c["notes"])}</aside>
</section>"""


def bullets(sid: str, c: dict) -> str:
    lines = "".join(f"<li>{esc(i)}</li>" for i in c["items"])
    return f"""<section id="{sid}">
{_kicker(c["kicker"])}
<h2>{esc(c["title"])}</h2>
<ul>{lines}</ul>
<aside>{esc(c["notes"])}</aside>
</section>"""


# Per layout: (render, required text keys, optional keys). `items` in bullets is a
# list of strings; `row` in table and leaderboard is the list of cell templates filled once
# per data row.
LAYOUTS: dict[str, tuple[Callable[[str, dict], str], set[str], set[str]]] = {
    "cover": (cover, {"kicker", "title", "subtitle", "notes"}, {"github"}),
    "big_number": (big_number, {"kicker", "number", "title", "punchline", "notes"}, set()),
    "big_name": (big_name, {"kicker", "winner", "title", "punchline", "notes"}, set()),
    "stat_row": (stat_row, {"kicker", "title", "items", "punchline", "notes"}, set()),
    "awards": (awards, {"kicker", "title", "cards", "punchline", "notes"}, set()),
    "table": (table, {"kicker", "title", "header", "row", "punchline", "notes"}, set()),
    "leaderboard": (leaderboard, {"kicker", "title", "row", "punchline", "notes"}, set()),
    "podium": (podium, {"kicker", "title", "row", "punchline", "notes"}, set()),
    "bullets": (bullets, {"kicker", "title", "items", "notes"}, set()),
}
# Layouts made of entries: (key, required keys, optional keys) of each entry. An entry may
# name its own `data`, which replaces the slide's; one whose data is missing is dropped.
ENTRIES: dict[str, tuple[str, set[str], set[str]]] = {
    "stat_row": ("items", {"value", "label"}, {"kicker", "data"}),
    "awards": ("cards", {"kicker", "winner", "stat", "punchline", "notes"}, {"data"}),
}
MAX_ENTRIES = 4
SLIDE_KEYS = {"id", "layout", "data", "enabled"}


# --- Config ----------------------------------------------------------------------------


def load_config(season: Season) -> dict:
    path = season.dir / "slides.toml"
    if not path.exists():
        raise SlideConfigError(f"{path} not found")
    return tomllib.loads(path.read_text())


def _check_entries(sl: dict, where: str) -> list[str]:
    key, required, optional = ENTRIES[sl["layout"]]
    errors = []
    entries = sl.get(key, [])
    if not 1 <= len(entries) <= MAX_ENTRIES:
        errors.append(f"{where}: `{key}` needs 1 to {MAX_ENTRIES} entries")
    for i, e in enumerate(entries, 1):
        for k in sorted(required - e.keys()):
            errors.append(f"{where}, {key} {i}: missing `{k}`")
        for k in sorted(e.keys() - required - optional):
            errors.append(f"{where}, {key} {i}: unknown key `{k}`")
        if "data" in e and e["data"] not in DATA:
            errors.append(f"{where}, {key} {i}: unknown data `{e['data']}`")
    return errors


def validate(config: dict) -> list[str]:
    """Structural problems in slides.toml (placeholders are checked when filled)."""
    errors = []
    for key in ("title", "slides"):
        if key not in config:
            errors.append(f"missing top-level `{key}`")
    for key in sorted(config.keys() - {"title", "slides"}):
        errors.append(f"unknown top-level key `{key}`")
    seen = set()
    for i, sl in enumerate(config.get("slides", []), 1):
        sid = sl.get("id", f"#{i}")
        where = f"slide {sid}"
        if "id" not in sl:
            errors.append(f"{where}: missing `id`")
        elif sid in seen:
            errors.append(f"{where}: duplicate id")
        seen.add(sid)
        for key in ("layout", "data"):
            if key not in sl:
                errors.append(f"{where}: missing `{key}`")
        if "data" in sl and sl["data"] not in DATA:
            errors.append(f"{where}: unknown data `{sl['data']}` (known: {', '.join(DATA)})")
        layout = LAYOUTS.get(sl.get("layout"))
        if layout is None:
            if "layout" in sl:
                errors.append(f"{where}: unknown layout `{sl['layout']}`")
            continue
        _, required, optional = layout
        for key in sorted(required - sl.keys()):
            errors.append(f"{where}: missing `{key}` for layout {sl['layout']}")
        for key in sorted(sl.keys() - required - optional - SLIDE_KEYS):
            errors.append(f"{where}: unknown key `{key}` for layout {sl['layout']}")
        if sl["layout"] in ENTRIES:
            errors += _check_entries(sl, where)
    return errors


def fill(template, values: Values, where: str):
    """Fill {placeholders} in a string, or in each string of a list or table of them."""
    if isinstance(template, list):
        return [fill(t, values, where) for t in template]
    if isinstance(template, dict):
        case = values.get("case")
        if case not in template:
            raise SlideConfigError(
                f"{where}: no variant for case `{case}` (has: {', '.join(template)})"
            )
        return fill(template[case], values, where)
    if not isinstance(template, str):
        return template
    try:
        return template.format_map(values)
    except KeyError as e:
        known = ", ".join(k for k in values if k not in ("case", "rows"))
        raise SlideConfigError(f"{where}: unknown placeholder {e} (known: {known})") from None
    except (ValueError, IndexError) as e:
        raise SlideConfigError(f"{where}: bad placeholder in {template!r}: {e}") from None


def render(sl: dict, values: Values, entry_values: list[Values] | None = None) -> str:
    """One slide's HTML. `entry_values` are the values of each of its items or cards, by
    default the slide's own."""
    sid = sl["id"]
    render_fn, required, optional = LAYOUTS[sl["layout"]]
    entry_key = ENTRIES.get(sl["layout"], ("",))[0]
    c = {}
    for key in required | (optional & sl.keys()):
        where = f"slide {sid}, `{key}`"
        if key == "row":
            c["rows"] = [fill(sl["row"], values | r, where) for r in values.get("rows", [])]
            c["data_rows"] = values.get("rows", [])
        elif key == entry_key:
            evs = entry_values or [values] * len(sl[key])
            c[key] = [
                {k: fill(v, ev, f"{where} {i}") for k, v in e.items() if k != "data"}
                for i, (e, ev) in enumerate(zip(sl[key], evs, strict=True), 1)
            ]
        else:
            c[key] = fill(sl[key], values, where)
    return render_fn(sid, c)


def entries(
    sl: dict, stats: Stats, season: Season, values: Values, log=print
) -> tuple[list[dict], list[Values]]:
    """A slide's items or cards that have data, with each one's values."""
    key = ENTRIES[sl["layout"]][0]
    kept, kept_values = [], []
    for i, e in enumerate(sl[key], 1):
        try:
            ev = values | DATA[e["data"]](stats, season) if "data" in e else values
        except NO_DATA as err:
            log(
                f"Skipping {key} {i} of slide {sl['id']}: no data ({err.__class__.__name__}: {err})"
            )
            continue
        kept.append(e)
        kept_values.append(ev)
    return kept, kept_values


# --- Build -----------------------------------------------------------------------------


def load_stats(season: Season) -> Stats:
    return {
        p.stem: pl.read_csv(p, try_parse_dates=True)
        for p in (season.gold_dir / "stats").glob("*.csv")
    }


def build(season: Season, log=print) -> list[str]:
    """Write <gold>/deck.html; returns the slide ids in order."""
    config = load_config(season)
    errors = validate(config)
    if errors:
        raise SlideConfigError("slides.toml:\n  " + "\n  ".join(errors))
    stats = load_stats(season)
    base = season_values(stats, season)

    pages, errors = {}, []
    for sl in config["slides"]:
        if not sl.get("enabled", True):
            continue
        try:
            values = base | DATA[sl["data"]](stats, season)
        except NO_DATA as e:
            log(f"Skipping slide {sl['id']}: no data ({e.__class__.__name__}: {e})")
            continue
        entry_values = None
        if sl["layout"] in ENTRIES:
            kept, entry_values = entries(sl, stats, season, values, log)
            if not kept:
                log(f"Skipping slide {sl['id']}: none of its entries have data")
                continue
            sl = sl | {ENTRIES[sl["layout"]][0]: kept}
        try:
            pages[sl["id"]] = render(sl, values, entry_values)
        except SlideConfigError as e:
            errors.append(str(e))
    if errors:
        raise SlideConfigError("slides.toml:\n  " + "\n  ".join(errors))

    title = fill(config["title"], base, "deck `title`")
    deck = local_deck(title, list(pages.values()), theme_css(log))
    (season.gold_dir / "deck.html").write_text(deck)
    return list(pages)


# --- Local deck ------------------------------------------------------------------------


def _asset(stem: str) -> str | None:
    """A data: URI for ASSETS/<stem>.<suffix>, or None if there is none."""
    for path in sorted(ASSETS.glob(f"{stem}.*")):
        if path.suffix.lower() in MIME:
            data = base64.b64encode(path.read_bytes()).decode()
            return f"data:{MIME[path.suffix.lower()]};base64,{data}"
    return None


def theme_css(log=print) -> str:
    """@font-face and slide background, embedding whichever theme assets are present."""
    font, picture = _asset(FONT_FILE), _asset(BACKGROUND_FILE)
    if font is None:
        log(f"No {ASSETS / FONT_FILE}.ttf: using Friz only if installed, else Georgia")
    if picture is None:
        log(f"No {ASSETS / BACKGROUND_FILE}.png: using a plain gradient background")
    src = "local('Friz Quadrata TT'), local('FrizQuadrataTT')"
    if font:
        src += f", url({font})"
    ground = f"url({picture}) center / cover" if picture else "radial-gradient(#2d4a22, #0b0f08)"
    return f"""
:root {{ --ink: {INK}; --muted: {MUTED}; --gold: {GOLD}; --orange: {ORANGE}; --rule: {RULE}; }}
@font-face {{ font-family: 'Friz Quadrata TT'; src: {src}; }}
section::before {{ background: {ground}; }}
"""


LOCAL_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; background: #070a08; overflow: hidden; }
section { position: absolute; left: 50%; top: 50%; width: 1920px; height: 1080px;
  transform-origin: 0 0; overflow: hidden; isolation: isolate; background: #0b0f08;
  padding: 120px 160px; display: flex; flex-direction: column; justify-content: center;
  color: var(--ink); font-family: 'Friz Quadrata TT', 'Friz Quadrata', Georgia, serif;
  font-weight: normal; font-synthesis: none; font-variant-numeric: lining-nums;
  text-shadow: 0 2px 6px #000, 0 0 2px #000; }
section:not(.current) { display: none; }
section aside { display: none; }
/* The picture, out of focus and bleeding past the edges so the blur has no fringe */
section::before { content: ""; position: absolute; inset: -60px; z-index: -2;
  filter: blur(14px) brightness(.8); }
/* One dimming for every slide, a little darker at the edges */
section::after { content: ""; position: absolute; inset: 0; z-index: -1;
  background: radial-gradient(ellipse at center, rgba(0,0,0,.45), rgba(0,0,0,.7)); }

/* Type. Friz's digits drop below the baseline, so numbers get room underneath only. */
h1, h2, h3, th { font-weight: normal; }
.kicker { font-size: 36px; line-height: 1.3; letter-spacing: 5px; text-transform: uppercase;
  color: var(--orange); margin-bottom: 28px; }
h1 { line-height: 1.1; color: var(--gold); }
h2 { font-size: 80px; line-height: 1.15; color: var(--gold); margin-bottom: 48px; }
.number { line-height: 1; padding-bottom: .15em; color: var(--gold); white-space: nowrap; }
.big-number .number { font-size: 300px; }
.big-name .number { font-size: 220px; }
.title { font-size: 52px; line-height: 1.3; }
.punch { font-size: 34px; line-height: 1.4; color: var(--muted); margin-top: 24px; }

.cover h1 { font-size: 180px; }
.cover .sub { font-size: 44px; color: var(--muted); margin-top: 24px; }
.github { position: absolute; left: 50%; bottom: 64px; transform: translateX(-50%);
  display: flex; align-items: center; gap: 14px; font-size: 26px; color: #8f8f8f;
  text-decoration: none; text-shadow: none; white-space: nowrap; }
.github svg { width: 30px; height: 30px; flex: none; }
.github:hover { color: #bdbdbd; }

.cells { display: flex; gap: 40px; }
.cells > div { flex: 1 1 0; min-width: 0; }
.cells .kicker, .card .kicker { font-size: 28px; letter-spacing: 3px; margin-bottom: 16px; }
.cells .number { font-size: 140px; }
.cells .label { font-size: 34px; line-height: 1.3; color: var(--muted); }

.awards h2 { font-size: 64px; margin-bottom: 40px; }
.cards { display: grid; gap: 40px; }
.card { min-width: 0; padding: 32px 40px; background: rgba(0,0,0,.35);
  border: 2px solid rgba(255,209,0,.3); border-radius: 6px; }
.card .kicker { font-size: 30px; }
.card .number { font-size: 120px; }
.card .stat { font-size: 44px; line-height: 1.3; }
.card .punch { font-size: 32px; margin-top: 16px; }
/* Four cards go two by two, so they get a little less room each */
.quad .card .number { font-size: 100px; }
.quad .card .stat { font-size: 40px; }
.quad .card .punch { font-size: 30px; margin-top: 8px; }

.leaderboard ol { list-style: none; }
.leaderboard li { display: grid; grid-template-columns: 60px 420px 400px 1fr 200px;
  align-items: center; gap: 28px; padding: 18px 0; border-bottom: 2px solid var(--rule); }
.leaderboard .rank { font-size: 36px; color: var(--muted); }
.leaderboard .who { font-size: 48px; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.leaderboard .detail { font-size: 30px; color: var(--muted); }
.leaderboard .bar { height: 28px; background: rgba(255,255,255,.08); }
.leaderboard .bar i { display: block; height: 100%; opacity: .85; }
.leaderboard .value { font-size: 48px; color: var(--gold); text-align: right; }

.steps { display: grid; grid-template-columns: repeat(3, 1fr); gap: 40px; align-items: end; }
.step { min-width: 0; text-align: center; }
.step .number { font-size: 100px; }
.step .label { font-size: 34px; color: var(--muted); margin-bottom: 24px; }
.plinth { background: rgba(0,0,0,.35); border: 2px solid rgba(255,209,0,.3);
  border-bottom: none; border-radius: 6px 6px 0 0; }
.p1 .plinth { height: 260px; }
.p2 .plinth { height: 180px; }
.p3 .plinth { height: 120px; }
.p2 .number { color: #c7ccd1; }
.p3 .number { color: #c98a4b; }

table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 12px 20px; }
th { font-size: 28px; color: var(--muted); border-bottom: 2px solid var(--gold); }
td { font-size: 32px; border-bottom: 2px solid var(--rule); }

ul { padding-left: 1.2em; max-width: 1500px; }
li { font-size: 38px; line-height: 1.4; margin-bottom: 20px; }
li::marker { color: var(--gold); }

#notes { position: fixed; left: 0; right: 0; bottom: 0; max-height: 30vh; overflow: auto;
  padding: 16px 24px; background: rgba(0,0,0,.85); color: #e8eee4;
  font: 18px/1.4 Georgia, serif; display: none; }
body.show-notes #notes { display: block; }
#count { position: fixed; right: 16px; bottom: 12px; color: #d3c6a6;
  font: 14px Georgia, serif; opacity: .6; }
@media print {
  @page { size: 1920px 1080px; margin: 0; }
  html, body { height: auto; overflow: visible; background: none; }
  section { position: relative; left: 0; top: 0; transform: none !important;
    break-after: page; }
  section:not(.current) { display: flex; }
  #notes, #count { display: none !important; }
}
"""

LOCAL_JS = """
const slides = [...document.querySelectorAll("section")];
const notes = document.getElementById("notes"), count = document.getElementById("count");
let i = Math.max(0, Math.min(slides.length - 1, (parseInt(location.hash.slice(1)) || 1) - 1));
function fit() {
  const s = Math.min(innerWidth / 1920, innerHeight / 1080);
  for (const el of slides) el.style.transform = `scale(${s}) translate(-50%, -50%)`;
}
function show(n) {
  i = Math.max(0, Math.min(slides.length - 1, n));
  slides.forEach((el, k) => el.classList.toggle("current", k === i));
  const aside = slides[i].querySelector("aside");
  notes.textContent = aside ? aside.textContent : "";
  count.textContent = `${i + 1} / ${slides.length}`;
  history.replaceState(null, "", `#${i + 1}`);
}
addEventListener("keydown", (e) => {
  if (["ArrowRight", "ArrowDown", "PageDown", " ", "Enter"].includes(e.key)) show(i + 1);
  else if (["ArrowLeft", "ArrowUp", "PageUp", "Backspace"].includes(e.key)) show(i - 1);
  else if (e.key === "Home") show(0);
  else if (e.key === "End") show(slides.length - 1);
  else if (e.key === "n") document.body.classList.toggle("show-notes");
  else if (e.key === "f") document.fullscreenElement
    ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  else return;
  e.preventDefault();
});
addEventListener("click", (e) => {
  if (!e.target.closest("a")) show(e.clientX < innerWidth / 3 ? i - 1 : i + 1);
});
addEventListener("resize", fit);
// Shrink a big value that overflows its line, down to half its size, then let it wrap
function shrink(el) {
  let size = parseFloat(getComputedStyle(el).fontSize);
  const min = size / 2;
  while (el.scrollWidth > el.clientWidth && size > min) el.style.fontSize = `${size -= 4}px`;
  if (el.scrollWidth > el.clientWidth) el.style.whiteSpace = "normal";
}
function shrinkAll() {
  for (const el of slides) {
    const shown = el.classList.contains("current");
    el.classList.add("current");
    el.querySelectorAll(".number").forEach(shrink);
    el.classList.toggle("current", shown);
  }
}
fit(); show(i);
document.fonts.ready.then(shrinkAll);
"""


def local_deck(title: str, pages: list[str], theme: str = "") -> str:
    body = "\n".join(pages)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{LOCAL_CSS}{theme}</style>
</head>
<body>
{body}
<div id="notes"></div>
<div id="count"></div>
<!-- Keys: arrows/space to move, N notes, F fullscreen; print for a PDF -->
<script>{LOCAL_JS}</script>
</body>
</html>
"""
