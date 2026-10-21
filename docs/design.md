# SGA design

How the pipeline turns Warcraft Logs reports into season stats and a deck. Terms are defined in [GLOSSARY.md](../GLOSSARY.md); the reasons behind the bigger choices are under [Decisions](#decisions). For setup and the season workflow see the [README](../README.md).

The season is open-ended: each run describes the season as of the latest night logged.

## Season inputs (`seasons/<slug>/`)

`season.toml`, hand-edited:

- season name, raid zone ID, timezone (default `Europe/London`) and night cutoff hour (default 06:00 local)
- the guild and the uploaders searched by `suggest-reports`
- the reports, each as its code and the raid night it covers; only listed reports count. All reports of a night are treated alike, and `build` warns when a date doesn't match the night a report's pulls fall on, or a report has no raid pulls
- character aliases (several characters → one player) and characters to leave out of everything
- the regular threshold (default 10% of pulls) and an optional `roster` of player names: when it isn't empty, only players on it can be regulars. `build` warns about roster names that match no player
- avoidable abilities: per boss in `[avoidable]`, and in `avoidable_non_tank` those that count on any boss but only for players not tanking the pull (boss melee, tank hits)

`slides.toml`, hand-edited: the deck's title and every slide in order, with its layout, data source and all of its text.

## Pipeline: medallion layers

### Bronze: fetch (`bronze.py`, `wcl.py`)

Raw GraphQL responses cached on disk under `.cache/bronze/` (git-ignored). Finished fights never change, so nothing is fetched twice. Per report:

- metadata, boss fights, actors and abilities
- death events; battle rez and Bloodlust-type casts; potion and healthstone casts (paginated, across all boss fights)
- trash fights in the raid's game zones (those of the season's bosses; a raid can span several) and one DamageDone table over them
- per boss fight: DamageDone, Healing, DamageTaken, Interrupts and Dispels tables plus `playerDetails` for roles and specs, five fights per request

Notes from the live API:

- `playerDetails`' `potionUse` and `healthstoneUse` are always 0, so consumables are counted from casts, with ability IDs picked by name from each report's ability list (potion IDs change every expansion).
- `wipeCalledTime` has been null on every wipe so far, so the fallback wipe call is what's actually used.
- The per-fight tables cost about 10 points per fight against 720 points per hour. `rateLimitData` lags behind large queries, so the client also keeps a reserve and, on HTTP 429, waits 5 minutes at a time (up to 75 minutes) before retrying.

### Silver: parse and merge (`silver.py`)

Polars tables stored as Parquet under `.cache/silver/<slug>/` (git-ignored), rebuilt on every `build`. Per-copy tables (`copy_fights`, `copy_player_fights`, `copy_deaths`, `copy_casts`, `copy_damage_taken`, `copy_trash`) and merged ones (`copies`, `pulls`, `player_pulls`, `deaths`, `casts`, `damage_taken`, `trash_nights`).

1. A pull is a boss fight from a listed report whose encounter belongs to the season's zone, at any difficulty.
2. Copies are the same pull when encounter and difficulty match and their absolute starts are within 20 s.
3. Pull merge: killed if any copy killed; lowest boss health %; earliest start to latest end.
4. Wipe call: Warcraft Logs' `wipeCalledTime`, else 10 s before the end.
5. Per-player merge: the max of each metric separately, never the sum; DPS and HPS recomputed from the merged amount and duration. Characters are matched by name and server; only player actors count.
6. Uploaders' clocks drift by several seconds, so each copy's events are shifted onto the pull's own timeline before matching. Deaths are then the union across copies, deduped as the same character within 2 s (keeping the earliest time and any killing blow); casts likewise per caster and ability.
7. Potions, healthstones and battle rezzes are counted per pull from the merged casts.
8. Nights are the local date in the season's timezone, with the day boundary at the cutoff hour.
9. Trash: per night, each uploader's reports are summed, then the max is taken across uploaders.

### Gold: stats (`gold.py`)

Each stat is a small Polars function over silver tables, registered in `STATS`. `build` writes `gold/stats/<name>.csv`, all stats in `gold/stats.json`, and the `pulls.csv` and `player_pulls.csv` ledgers that keep every stat traceable after the cache is cleared. Gold is committed to git.

- Awards and leaderboards consider regulars only; season totals include everyone.
- Ties are broken by player, boss, ability, night and pull, so the same logs always give the same output and winners.
- Post-call deaths count in death totals but not in first-to-die or fastest-death stats. Deaths in the first second of a pull are treated as having started before it.
- Generic boss melee is left out of killing-blow stats.

| Stat | Definition |
| --- | --- |
| Season totals | Nights, pulls, kills, wipes, deaths, boss combat hours, hours on wipes |
| Progression | Per boss and difficulty: first kill night, pulls up to and including it, best boss % |
| Nemesis boss | Most-pulled boss and difficulty; best boss % on each night |
| So close | Wipes with the least boss health left |
| Speedruns | Shortest wipes; fastest pre-call death from pull start |
| Killing blows | Abilities that killed the most players; each player's personal nemesis ability |
| Toughest night | Deaths, pulls and kills per night |
| Kill cleanliness | Deaths on each kill: the messiest and the deathless ones |
| Floor awards | First to die (pre-call), in total and per pull; deaths per pull |
| Attendance | Pulls attended out of all pulls |
| Damage and healing | Season totals; best whole-night DPS; most damage taken by a non-tank |
| Interrupts, dispels | Most of each |
| Battle rezzes | Most given and received; a Soulstone counts for the player rezzed |
| Consumables | Potions and healthstones used; who never used either |
| Bloodlust timing | Median and mean time into the pull of the first lust |
| Avoidable damage | Damage from approved abilities per pull attended; non-tank-only abilities skip pulls the player tanked. Main role recorded so non-tanks can be compared |
| The Janitor | Damage to raid trash, on the nights someone logged trash |
| Cockroach | Wipes still alive at the end; Holy Priest pulls left out (Spirit of Redemption hides their deaths) |
| Jack of all trades | Specs, roles and classes played on boss pulls |

### Slides (`slides.py`)

`sga slides` fills the slides declared in `slides.toml` from gold and writes one standalone `gold/deck.html`. Nothing is hosted.

- Each `[[slides]]` entry has an `id`, a `layout` (`cover`, `big_number`, `stat_row`, `table`, `bullets`), a `data` source, and that layout's text keys. `{placeholders}` come from the slide's data function plus season-wide values.
- Text that depends on the numbers is a table of variants keyed by the data's `case`, e.g. `number = { perfect = "...", top = "..." }`.
- A missing or unknown key, an unknown placeholder or a missing variant fails the build with every problem listed. A slide whose stat is missing (no avoidable abilities approved yet, say) is skipped with a message.
- Slides are 1920×1080, scaled to the window. Dark background, venom green and amber, Oswald and Rubik from Google Fonts (Arial offline). Speaker notes are hidden until N is pressed but are in the file, so they hold only definitions, exact numbers and sources.
- Printing gives one slide per page.

## Helpers (`suggest.py`)

- `suggest-reports`: reports in the season's zone from the guild and the named uploaders that aren't listed yet, from a day before the first listed report until now. An uploader is only searched once one of their reports is listed (their user ID comes from it).
- `suggest-avoidable`: per boss, enemy abilities scored by how few non-tanks they hit and how unevenly. A person decides which are avoidable.

## Decisions

- **Merge copies by per-metric max, not events.** Each copy of a pull can miss part of a player's activity, so per-player values take the max across copies and are never summed. This can still undercount when two loggers missed different parts of the same player, but merging raw damage and healing events would cost far more API points against the 720-point hourly limit. Deaths and casts are the exception: they are few and cheap, so they're merged per event after aligning the copies' clocks. Revisit only if the max merge proves materially wrong.
- **Medallion layers.** Bronze keeps the API's responses untouched so nothing is fetched twice; silver is the stable interface, so a new stat is one Polars function over silver and never touches fetching or merging. Only gold goes into git. Bronze and silver are deleted at season end, so the gold ledgers keep every number traceable afterwards.
- **The deck is declared in `slides.toml`, with no wording in code.** It replaced optional overrides on top of hard-coded text, where overriding a title silently dropped its numbers and typos were ignored. It stops at text and structure: binding slides to tables or declaring stats in TOML was rejected because the awkward logic (ties, exclusions, joined lists) is clearer as Python. So a new award still needs a gold stat and a data function.

## Tests

`uv run pytest`, needing no API or cache: merging and a few stat rules on a tiny fake bronze cache (`test_pipeline.py`), and `slides.toml` validation (`test_slides.py`).
