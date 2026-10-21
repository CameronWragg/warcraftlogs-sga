# SGA: Seasonal Guild Aggregator

Turns one guild's Warcraft Logs (retail) raid reports for a season into season-wide stats and a slide deck of "fun stat" awards for the guild meeting. The deck is a single local HTML file: open it in a browser to present, or print it to PDF.

- How it works and why: [docs/design.md](docs/design.md)
- Domain terms (pull, copy, regular, wipe call…): [GLOSSARY.md](GLOSSARY.md)

## Setup

1. Open the folder in VS Code and reopen it in the devcontainer. It installs Python 3.14 and the dependencies with `uv sync`.
2. Create a Warcraft Logs API client at <https://www.warcraftlogs.com/api/clients>, copy `.env.example` to `.env` and fill in `WCL_CLIENT_ID` and `WCL_CLIENT_SECRET`. `.env` is git-ignored.

Run commands in the container's terminal, or from the host with `npx @devcontainers/cli exec --workspace-folder . uv run sga …`.

## A season, step by step

Each season lives in `seasons/<slug>/`. Commands take the slug, e.g. `uv run sga build venomous-abyss`.

1. **Describe the season** in `season.toml`: the raid zone ID, the guild, and the uploaders whose personal logs may hold guild nights. List each report to include in `reports` with its code and the raid night it covers: only listed reports count, and several reports of the same night are all merged. `build` warns if a date doesn't match the report.
2. **Find reports you missed**: `sga suggest-reports <slug>` lists unlisted reports in the zone from the guild and the named uploaders. It never edits `season.toml`; add the ones that are guild nights by hand.
3. **Fetch**: `sga fetch <slug>` downloads everything into `.cache/bronze/`. It waits out the API's hourly rate limit by itself, so the first run of a full season takes hours (~10 points per boss pull, 720 points an hour). Re-runs only fetch what is missing; interrupt and resume freely.
4. **Build**: `sga build <slug>` merges duplicate logs and computes the stats into `seasons/<slug>/gold/`.
5. **Approve avoidable damage**: `sga suggest-avoidable <slug>` ranks abilities per boss that hit a few players hard. Add the ones that really are avoidable to `[avoidable]` in `season.toml` (or `avoidable_non_tank` for things like boss melee), then build again.
6. **Write the deck** in `slides.toml`: the order of slides and all of their text, with `{placeholders}` for the numbers. Set `enabled = false` on any award that could sting.
7. **Generate**: `sga slides <slug>` writes `seasons/<slug>/gold/deck.html`. Keys: arrows/space/click to move, N for speaker notes, F for fullscreen.

During a season, add new reports to `season.toml` and repeat steps 3, 4 and 7.

### Starting a new season

Copy the previous season's folder without its `gold/`, then update `season.toml` (name, zone ID, reports, aliases, avoidable abilities) and adjust `slides.toml`. Once the old season is finished, delete `.cache/` to clear its raw logs; the old season's `gold/` (stats, ledgers and deck) stays in git.

## Development

```sh
uv run pytest                      # unit tests on a tiny fake log cache; no API or cache needed
uv run ruff check                  # lint (rules in pyproject.toml)
uv run ruff format                 # format
uv run pre-commit run --all-files  # every commit hook, on all tracked files
```

The devcontainer installs the pre-commit hooks, which run ruff (lint with fixes, then format) and basic file checks on every commit. Outside the container, run `uv run pre-commit install` once.

Code lives in `src/sga/`: `wcl.py` (API client), `bronze.py` (fetch and cache), `silver.py` (parse and merge), `gold.py` (stats), `slides.py` (deck), `suggest.py` (helpers), `cli.py`. A new award needs a stat function in `gold.py`, a data function in `slides.py` and a slide entry in `slides.toml`.
