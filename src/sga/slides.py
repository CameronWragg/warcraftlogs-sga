"""Slides: write <gold>/deck.html from gold stats and seasons/<slug>/slides.toml.

slides.toml declares every slide and all of its text (docs/design.md). This module supplies
the theme, the LAYOUTS, and per slide a DATA function returning values for {placeholders}.
Nothing has a default: a missing or unknown key, an unknown placeholder or a missing case
variant fails the build. Each slide is a 1920×1080 <section>; notes go in its <aside>.

  [[slides]]
  id = "nemesis"          # the slide's id, unique in the deck
  layout = "big_number"   # see LAYOUTS for each layout's keys
  data = "nemesis"        # a key of DATA; season_values are available to every slide
  enabled = true          # optional; false drops the slide
  kicker = "The nemesis"
  number = "{pulls}"
  title = "pulls on {boss}, {hours} hours of it"
  punchline = "UWUTEK."
  notes = "Most-pulled boss and difficulty. Nightly best boss %: {trail}."

Text that depends on the numbers picks a variant: a data function returns a `case`, and any
text key may be a table keyed by case, e.g. punchline = { perfect = "...", top = "..." }.
Table rows work the same way with each row's own `case`.
"""

import html
import tomllib
from collections.abc import Callable
from datetime import date

import polars as pl

from sga.season import Season

# Theme: dark with venom green and amber accents
BG = "#0f1511"
SURFACE = "#18211b"
INK = "#e8eee4"
MUTED = "#9aa89d"
GREEN = "#93d14f"
AMBER = "#f2a93b"
DISPLAY = "Oswald, 'Arial Narrow', sans-serif"
BODY = "Rubik, Arial, sans-serif"
FONTS = (
    "https://fonts.googleapis.com/css2?family=Oswald:wght@400..700"
    "&family=Rubik:ital,wght@0,300..700;1,300..700&display=swap"
)

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
                "boss": r["boss"],
                "first_kill": day(r["first_kill_night"]) if r["first_kill_night"] else "",
                "pulls_to_kill": n(r["pulls_to_first_kill"]) if r["pulls_to_first_kill"] else "",
                "total_pulls": n(r["pulls"]),
                "best_pct": pct(r["best_boss_pct"]),
            }
            for r in p.iter_rows(named=True)
        ]
    }


def d_nemesis(st, s) -> Values:
    b = row(st, "nemesis_boss")
    trail = " → ".join(
        "KILL" if r["killed"] else pct(r["best_boss_pct"])
        for r in st["nemesis_by_night"].iter_rows(named=True)
    )
    return {
        "pulls": n(b["pulls"]),
        "boss": b["boss"],
        "hours": f"{b['combat_hours']:.1f}",
        "trail": trail,
    }


def d_so_close(st, s) -> Values:
    r = row(st, "so_close")
    return {
        "boss_pct": pct(r["boss_pct"]),
        "boss": r["boss"],
        "night": day(r["night"]),
        "next_closest": ", ".join(
            f"{x['boss']} {pct(x['boss_pct'])} ({day(x['night'])})"
            for x in st["so_close"].slice(1, 4).iter_rows(named=True)
        ),
    }


def d_speedrun(st, s) -> Values:
    r = row(st, "shortest_wipes")
    return {
        "duration": mmss(r["duration_ms"]),
        "seconds": f"{r['duration_ms'] / 1000:.1f}",
        "boss": r["boss"],
        "night": day(r["night"]),
        "pull_id": r["pull_id"],
    }


def d_fastest_death(st, s) -> Values:
    r = row(st, "fastest_deaths")
    return {
        "case": "ability" if r["ability"] else "no_ability",
        "seconds": f"{r['since_pull_ms'] / 1000:.1f}",
        "player": r["player"],
        "ability": r["ability"] or "",
        "boss": r["boss"],
        "night": day(r["night"]),
        "pull_id": r["pull_id"],
    }


def d_killing_blow(st, s) -> Values:
    r = row(st, "top_killing_blows")
    return {
        "deaths": n(r["deaths"]),
        "ability": r["ability"],
        "bosses": r["bosses"],
        "next": ", ".join(
            f"{x['ability']} {x['deaths']}"
            for x in st["top_killing_blows"].slice(1, 4).iter_rows(named=True)
        ),
    }


def d_toughest_night(st, s) -> Values:
    nights = st["nights"]
    r = nights.sort("deaths", descending=True, maintain_order=True).row(0, named=True)
    return {
        "deaths": n(r["deaths"]),
        "night": day(r["night"]),
        "pulls": r["pulls"],
        "kills": r["kills"],
        "zero_kill_nights": nights.filter(pl.col("kills") == 0).height,
    }


def d_cleanest_kills(st, s) -> Values:
    k = st["kills_by_deaths"]
    return {"clean": k.filter(pl.col("deaths") == 0).height, "kills": k.height}


def d_ugliest_kill(st, s) -> Values:
    r = row(st, "kills_by_deaths")
    return {
        "deaths": n(r["deaths"]),
        "boss": r["boss"],
        "night": day(r["night"]),
        "pull_id": r["pull_id"],
    }


def d_floor_award(st, s) -> Values:
    f = st["floor_awards"]
    top = f.row(0, named=True)
    rate = f.sort("first_death_rate", descending=True, maintain_order=True).row(0, named=True)
    return {
        "player": top["player"],
        "first_deaths": top["first_deaths"],
        "rate_player": rate["player"],
        "rate": f"{rate['first_death_rate']:.0%}",
    }


def d_attendance(st, s) -> Values:
    a = st["attendance"]
    total = a.row(0, named=True)["of_pulls"]
    perfect = a.filter(pl.col("pulls_attended") == total)["player"].to_list()
    top = a.row(0, named=True)
    return {
        "case": "perfect" if perfect else "top",
        "perfect_players": " & ".join(perfect),
        "player": top["player"],
        "attended": n(top["pulls_attended"]),
        "total": n(total),
    }


def d_damage_healing(st, s) -> Values:
    t = row(st, "damage_healing_totals")
    return {
        "damage": big(t["damage_done"]),
        "healing": big(t["healing"]),
        "damage_exact": n(t["damage_done"]),
        "healing_exact": n(t["healing"]),
    }


def d_best_night_dps(st, s) -> Values:
    r = row(st, "best_night_dps")
    return {"dps": big(r["dps"]), "player": r["player"], "night": day(r["night"])}


def d_damage_sponge(st, s) -> Values:
    r = row(st, "most_damage_taken_non_tank")
    return {"player": r["player"], "damage_taken": big(r["damage_taken"])}


def d_avoidable(st, s) -> Values:
    a = st["avoidable_damage"]
    top = a.row(0, named=True)
    # Tanks are exempt from most of the list, so the cleanest feet go to a non-tank
    clean = a.filter(pl.col("main_role") != "tank").row(-1, named=True)
    return {
        "player": top["player"],
        "per_pull": big(top["per_pull"]),
        "clean_player": clean["player"],
        "clean_per_pull": big(clean["per_pull"]),
        "sources": ", ".join(
            f"{x['ability']} ({x['boss']}) {big(x['avoidable_damage'])}"
            for x in st["avoidable_by_ability"].head(8).iter_rows(named=True)
        ),
    }


def d_cookie_monster(st, s) -> Values:
    r = (
        st["consumables"]
        .sort("healthstones", descending=True, maintain_order=True)
        .row(0, named=True)
    )
    return {
        "player": r["player"],
        "healthstones": n(r["healthstones"]),
        "per_pull": f"{r['healthstones'] / r['pulls']:.1f}",
        "pulls": n(r["pulls"]),
    }


def d_janitor(st, s) -> Values:
    r = row(st, "janitor")
    cov = row(st, "trash_coverage")
    return {
        "player": r["player"],
        "trash_damage": big(r["trash_damage"]),
        "nights_with_trash": cov["nights_with_trash"],
        "nights": cov["nights"],
    }


def d_cockroach(st, s) -> Values:
    r = row(st, "cockroach")
    return {
        "player": r["player"],
        "survived": r["survived"],
        "wipes": r["wipes"],
        "rate": f"{r['survival_rate']:.0%}",
    }


def d_jack_of_all_trades(st, s) -> Values:
    r = row(st, "jack_of_all_trades")
    return {
        "player": r["player"],
        "specs": r["specs"],
        "roles": r["roles"],
        "spec_list": r["spec_list"],
    }


def d_interrupts(st, s) -> Values:
    r = row(st, "interrupts")
    return {"player": r["player"], "interrupts": n(r["interrupts"])}


def d_dispels(st, s) -> Values:
    r = row(st, "dispels")
    return {"player": r["player"], "dispels": n(r["dispels"])}


def d_battle_rez(st, s) -> Values:
    g = row(st, "rezzes_given")
    r = row(st, "rezzes_received")
    return {
        "giver": g["player"],
        "given": g["rezzes_given"],
        "receiver": r["player"],
        "received": r["rezzes_received"],
    }


def d_consumables(st, s) -> Values:
    t = st["consumables"]
    top = t.sort("potions", descending=True, maintain_order=True).row(0, named=True)
    never = t.filter((pl.col("potions") == 0) & (pl.col("healthstones") == 0))["player"]
    return {
        "case": "some_never" if never.len() else "all_pressed",
        "player": top["player"],
        "potions": n(top["potions"]),
        "never": ", ".join(never.to_list()),
    }


def d_lust_timing(st, s) -> Values:
    r = row(st, "lust_timing")
    return {
        "case": "mean_above_median" if r["avg_seconds"] > r["median_seconds"] else "mean_below",
        "median": f"{r['median_seconds']:.0f}",
        "mean": f"{r['avg_seconds']:.0f}",
        "median_exact": r["median_seconds"],
        "mean_exact": r["avg_seconds"],
        "pulls_with_lust": r["pulls_with_lust"],
    }


DATA: dict[str, Callable[[Stats, Season], Values]] = {
    "none": d_none,
    "progression": d_progression,
    "nemesis": d_nemesis,
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
    "best_night_dps": d_best_night_dps,
    "damage_sponge": d_damage_sponge,
    "avoidable": d_avoidable,
    "cookie_monster": d_cookie_monster,
    "janitor": d_janitor,
    "cockroach": d_cockroach,
    "jack_of_all_trades": d_jack_of_all_trades,
    "interrupts": d_interrupts,
    "dispels": d_dispels,
    "battle_rez": d_battle_rez,
    "consumables": d_consumables,
    "lust_timing": d_lust_timing,
}


# --- Layouts ---------------------------------------------------------------------------
# Each takes the slide id and its resolved config (all text already filled in).


def _kicker(text: str, color: str, size: int = 40, spacing: int = 4) -> str:
    return (
        f'<h3 style="font-family:{DISPLAY};font-size:{size}px;font-weight:500;'
        f'letter-spacing:{spacing}px;color:{color}">{esc(text).upper()}</h3>'
    )


def _palette(statement: bool) -> tuple[str, str, str, str, str]:
    """(background, ink, accent, muted, kicker) for a normal or full-colour statement slide."""
    return (GREEN, BG, BG, "#24331f", BG) if statement else (BG, INK, GREEN, MUTED, AMBER)


def cover(sid: str, c: dict) -> str:
    return f"""<section id="{sid}" style="background:{BG};color:{INK};font-family:{BODY};padding:128px;display:flex;flex-direction:column;justify-content:flex-end;gap:32px">
{_kicker(c["kicker"], AMBER, 44, 6)}
<h1 style="font-family:{DISPLAY};font-size:200px;font-weight:700;line-height:0.95;color:{GREEN}">{esc(c["title"])}</h1>
<p style="font-size:44px;color:{MUTED}">{esc(c["subtitle"])}</p>
<aside>{esc(c["notes"])}</aside>
</section>"""


def big_number(sid: str, c: dict) -> str:
    bg, ink, accent, muted, kicker = _palette(c.get("statement", False))
    return f"""<section id="{sid}" data-transition="fade" style="background:{bg};color:{ink};font-family:{BODY};padding:128px;display:flex;flex-direction:column;justify-content:center;gap:24px">
{_kicker(c["kicker"], kicker)}
<h1 style="font-family:{DISPLAY};font-size:{c.get("number_size", 300)}px;font-weight:700;line-height:1;color:{accent}">{esc(c["number"])}</h1>
<p style="font-size:52px;line-height:1.25;font-weight:500">{esc(c["title"])}</p>
<p style="font-size:36px;line-height:1.3;font-style:italic;color:{muted}">{esc(c["punchline"])}</p>
<aside>{esc(c["notes"])}</aside>
</section>"""


def stat_row(sid: str, c: dict) -> str:
    """Up to four big numbers side by side."""
    bg, ink, accent, muted, kicker = _palette(c.get("statement", False))
    cells = "".join(
        f'<div style="flex:1;display:flex;flex-direction:column;gap:8px">'
        f'<p style="font-family:{DISPLAY};font-size:150px;font-weight:700;line-height:1;color:{accent}">{esc(i["value"])}</p>'
        f'<p style="font-size:34px;color:{muted}">{esc(i["label"])}</p></div>'
        for i in c["items"]
    )
    return f"""<section id="{sid}" data-transition="fade" style="background:{bg};color:{ink};font-family:{BODY};padding:128px;display:flex;flex-direction:column;justify-content:center;gap:56px">
{_kicker(c["kicker"], kicker)}
<h2 style="font-family:{DISPLAY};font-size:96px;font-weight:600;line-height:1.05">{esc(c["title"])}</h2>
<div style="display:flex;gap:48px">{cells}</div>
<p style="font-size:36px;font-style:italic;color:{muted}">{esc(c["punchline"])}</p>
<aside>{esc(c["notes"])}</aside>
</section>"""


def table(sid: str, c: dict) -> str:
    th = "".join(
        f'<th style="text-align:left;padding:12px 20px;font-size:28px;color:{MUTED};font-weight:500;border-bottom:2px solid {GREEN}">{esc(h)}</th>'
        for h in c["header"]
    )
    trs = "".join(
        "<tr>"
        + "".join(
            f'<td style="padding:12px 20px;font-size:32px;border-bottom:1px solid {SURFACE}">{esc(cell)}</td>'
            for cell in r
        )
        + "</tr>"
        for r in c["rows"]
    )
    return f"""<section id="{sid}" data-transition="fade" style="background:{BG};color:{INK};font-family:{BODY};padding:128px;display:flex;flex-direction:column;gap:32px">
{_kicker(c["kicker"], AMBER)}
<h2 style="font-family:{DISPLAY};font-size:80px;font-weight:600;line-height:1.05">{esc(c["title"])}</h2>
<table style="width:1664px;border-collapse:collapse"><tr>{th}</tr>{trs}</table>
<p style="font-size:32px;font-style:italic;color:{MUTED}">{esc(c["punchline"])}</p>
<aside>{esc(c["notes"])}</aside>
</section>"""


def bullets(sid: str, c: dict) -> str:
    lines = "".join(
        f'<p style="font-size:36px;line-height:1.35">• {esc(i)}</p>' for i in c["items"]
    )
    return f"""<section id="{sid}" style="background:{BG};color:{INK};font-family:{BODY};padding:128px;display:flex;flex-direction:column;gap:48px">
{_kicker(c["kicker"], AMBER)}
<h2 style="font-family:{DISPLAY};font-size:96px;font-weight:600">{esc(c["title"])}</h2>
<div style="display:flex;flex-direction:column;gap:24px">{lines}</div>
<aside>{esc(c["notes"])}</aside>
</section>"""


# Per layout: (render, required text keys, optional non-text keys). `items` in stat_row is a
# list of {value, label} tables; in bullets a list of strings. `row` in table is the list of
# cell templates filled once per data row.
LAYOUTS: dict[str, tuple[Callable[[str, dict], str], set[str], set[str]]] = {
    "cover": (cover, {"kicker", "title", "subtitle", "notes"}, set()),
    "big_number": (
        big_number,
        {"kicker", "number", "title", "punchline", "notes"},
        {"statement", "number_size"},
    ),
    "stat_row": (stat_row, {"kicker", "title", "items", "punchline", "notes"}, {"statement"}),
    "table": (table, {"kicker", "title", "header", "row", "punchline", "notes"}, set()),
    "bullets": (bullets, {"kicker", "title", "items", "notes"}, set()),
}
SLIDE_KEYS = {"id", "layout", "data", "enabled"}


# --- Config ----------------------------------------------------------------------------


def load_config(season: Season) -> dict:
    path = season.dir / "slides.toml"
    if not path.exists():
        raise SlideConfigError(f"{path} not found")
    return tomllib.loads(path.read_text())


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
    return errors


def fill(template, values: Values, where: str):
    """Fill {placeholders} in a string, or in each string of a list or table of them."""
    if isinstance(template, list):
        return [fill(t, values, where) for t in template]
    if isinstance(template, dict):
        if set(template) == {"value", "label"}:
            return {k: fill(v, values, where) for k, v in template.items()}
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


def render(sl: dict, values: Values) -> str:
    sid = sl["id"]
    render_fn, required, optional = LAYOUTS[sl["layout"]]
    c = {}
    for key in required | (optional & sl.keys()):
        where = f"slide {sid}, `{key}`"
        if key == "row":
            c["rows"] = [fill(sl["row"], values | r, where) for r in values.get("rows", [])]
        else:
            c[key] = fill(sl[key], values, where)
    return render_fn(sid, c)


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
        except (KeyError, IndexError, pl.exceptions.PolarsError) as e:
            # No data, e.g. no avoidable abilities approved yet
            log(f"Skipping slide {sl['id']}: no data ({e.__class__.__name__}: {e})")
            continue
        try:
            pages[sl["id"]] = render(sl, values)
        except SlideConfigError as e:
            errors.append(str(e))
    if errors:
        raise SlideConfigError("slides.toml:\n  " + "\n  ".join(errors))

    title = fill(config["title"], base, "deck `title`")
    (season.gold_dir / "deck.html").write_text(local_deck(title, list(pages.values())))
    return list(pages)


# --- Local deck ------------------------------------------------------------------------

LOCAL_CSS = """
* { margin: 0; box-sizing: border-box; }
html, body { height: 100%; background: #070a08; overflow: hidden; }
section { position: absolute; left: 50%; top: 50%; width: 1920px; height: 1080px;
  transform-origin: 0 0; overflow: hidden; }
/* Slides set their own display inline, so hiding needs !important */
section:not(.current) { display: none !important; }
section aside { display: none; }
ul, ol { padding-left: 1.2em; }
table { border-collapse: collapse; }
#notes { position: fixed; left: 0; right: 0; bottom: 0; max-height: 30vh; overflow: auto;
  padding: 16px 24px; background: rgba(0,0,0,.85); color: #e8eee4;
  font: 18px/1.4 Rubik, Arial, sans-serif; display: none; }
body.show-notes #notes { display: block; }
#count { position: fixed; right: 16px; bottom: 12px; color: #9aa89d;
  font: 14px Rubik, Arial, sans-serif; opacity: .6; }
@media print {
  @page { size: 1920px 1080px; margin: 0; }
  html, body { height: auto; overflow: visible; background: none; }
  section { position: relative; left: 0; top: 0; transform: none !important;
    break-after: page; }
  section:not(.current) { display: flex !important; }
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
addEventListener("click", (e) => show(e.clientX < innerWidth / 3 ? i - 1 : i + 1));
addEventListener("resize", fit);
fit(); show(i);
"""


def local_deck(title: str, pages: list[str]) -> str:
    body = "\n".join(pages)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<link rel="stylesheet" href="{FONTS}">
<style>{LOCAL_CSS}</style>
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
