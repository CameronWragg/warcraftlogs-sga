"""Season inputs: seasons/<slug>/season.toml (the deck itself is slides.toml, see slides.py)."""

import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

SEASONS = Path("seasons")


@dataclass(frozen=True)
class ReportRef:
    code: str
    date: date  # the raid night the report covers


@dataclass(frozen=True)
class Season:
    slug: str
    name: str
    zone_id: int
    timezone: str
    night_cutoff_hour: int
    regular_threshold: float
    guild: dict
    uploaders: list[str]
    reports: list[ReportRef]
    # player name → characters ("Name-Server")
    aliases: dict[str, list[str]] = field(default_factory=dict)
    excluded_characters: list[str] = field(default_factory=list)
    # encounter ID → approved avoidable ability IDs
    avoidable: dict[int, list[int]] = field(default_factory=dict)
    # ability IDs avoidable on every encounter, but only for players not tanking the pull
    avoidable_non_tank: list[int] = field(default_factory=list)
    # player names allowed to be regulars; empty means everyone
    roster: list[str] = field(default_factory=list)

    @property
    def dir(self) -> Path:
        return SEASONS / self.slug

    @property
    def silver_dir(self) -> Path:
        return Path(".cache/silver") / self.slug

    @property
    def gold_dir(self) -> Path:
        return self.dir / "gold"


def load(slug: str) -> Season:
    raw = tomllib.loads((SEASONS / slug / "season.toml").read_text())
    return Season(
        slug=slug,
        name=raw["name"],
        zone_id=raw["zone_id"],
        timezone=raw.get("timezone", "Europe/London"),
        night_cutoff_hour=raw.get("night_cutoff_hour", 6),
        regular_threshold=raw.get("regular_threshold", 0.10),
        guild=raw.get("guild", {}),
        uploaders=raw.get("uploaders", []),
        reports=[ReportRef(**r) for r in raw.get("reports", [])],
        aliases=raw.get("aliases", {}),
        excluded_characters=raw.get("exclude", {}).get("characters", []),
        avoidable={int(k): v for k, v in raw.get("avoidable", {}).items()},
        avoidable_non_tank=raw.get("avoidable_non_tank", []),
        roster=raw.get("roster", []),
    )
