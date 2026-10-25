"""Bronze layer: raw GraphQL responses, cached forever on disk.

Layout under .cache/bronze/:
  zones/<zone_id>.json           zone metadata with its encounter list
  reports/<code>/report.json     metadata, boss fights, actors, abilities
  reports/<code>/deaths.json     every death event across the report's boss fights
  reports/<code>/casts.json      battle rez and Bloodlust-type casts across boss fights
  reports/<code>/consumables.json  potion and healthstone casts across boss fights
  reports/<code>/trash.json      raid-zone trash fights and their DamageDone table
  reports/<code>/fights/<id>.json  per-fight tables and player details
"""

import json
import re
import time
from pathlib import Path

from sga.wcl import WclClient

BRONZE = Path(".cache/bronze")

# Casts fetched per report. Changing this list means deleting casts.json to refetch.
BATTLE_REZ_ABILITIES = {
    20484: "Rebirth",
    61999: "Raise Ally",
    391054: "Intercession",
    95750: "Soulstone Resurrection",
}
LUST_ABILITIES = {
    2825: "Bloodlust",
    32182: "Heroism",
    80353: "Time Warp",
    264667: "Primal Rage",
    390386: "Fury of the Aspects",
}
CAST_ABILITIES = BATTLE_REZ_ABILITIES | LUST_ABILITIES

# Potion IDs change every expansion, so consumables are picked by name from each report's
# ability list. playerDetails' potionUse/healthstoneUse fields are always 0.
POTION = re.compile(r"\bPotion\b", re.IGNORECASE)
HEALTHSTONE = re.compile(r"^(Demonic )?Healthstone$", re.IGNORECASE)


def consumable_kind(ability_name: str) -> str | None:
    if HEALTHSTONE.search(ability_name):
        return "healthstone"
    if POTION.search(ability_name):
        return "potion"
    return None


FIGHT_TABLES = {
    "damage_done": "DamageDone",
    "healing": "Healing",
    "damage_taken": "DamageTaken",
    "interrupts": "Interrupts",
    "dispels": "Dispels",
}
FIGHTS_PER_REQUEST = 5
# A report that ended this recently may still be live-logging: nothing of it is cached yet,
# since the cache is never refreshed
LIVE_MS = 3_600_000

REPORT_FIELDS = """
  code title startTime endTime
  owner { id name }
  guild { id name server { slug region { slug } } }
  zone { id name }
  fights(killType: Encounters) {
    id encounterID name difficulty kill size
    startTime endTime bossPercentage fightPercentage wipeCalledTime
  }
  masterData {
    actors { id name server type subType petOwner }
    abilities { gameID name type icon }
  }
"""


def _report(code: str, fields: str) -> str:
    return f'reportData {{ report(code: "{code}") {{ {fields} }} }}'


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def read(path: Path):
    return json.loads(path.read_text())


def report_dir(code: str) -> Path:
    return BRONZE / "reports" / code


def fetch_zone(client: WclClient, zone_id: int) -> dict:
    path = BRONZE / "zones" / f"{zone_id}.json"
    if not path.exists():
        data = client.query(
            f"worldData {{ zone(id: {zone_id}) {{ id name encounters {{ id name }} }} }}"
        )
        _write(path, data["worldData"]["zone"])
    return read(path)


def _paginate_events(client: WclClient, code: str, args: str) -> list[dict]:
    events, start = [], None
    while True:
        start_arg = f", startTime: {start}" if start is not None else ""
        data = client.query(
            _report(code, f"events({args}{start_arg}) {{ data nextPageTimestamp }}")
        )["reportData"]["report"]["events"]
        events.extend(data["data"])
        start = data["nextPageTimestamp"]
        if start is None:
            return events


def _fetch_trash(client: WclClient, code: str, raid_encounters: set[int]) -> dict:
    """Trash fights in the same game zone as the raid's boss fights, and their damage done."""
    fights = client.query(
        _report(
            code,
            "boss: fights(killType: Encounters) { id encounterID gameZone { id } } "
            "trash: fights(killType: Trash) { id startTime endTime gameZone { id } }",
        )
    )["reportData"]["report"]
    raid_zones = {
        f["gameZone"]["id"]
        for f in fights["boss"]
        if f.get("gameZone") and f["encounterID"] in raid_encounters
    }
    trash = [f for f in fights["trash"] if (f.get("gameZone") or {}).get("id") in raid_zones]
    damage_done = {}
    if trash:
        ids = [f["id"] for f in trash]
        damage_done = client.query(_report(code, f"table(dataType: DamageDone, fightIDs: {ids})"))[
            "reportData"
        ]["report"]["table"]["data"]
    return {"fights": trash, "damage_done": damage_done}


def fetch_report(client: WclClient, code: str, raid_encounters: set[int], log=print) -> None:
    """Fetch everything bronze needs for one report. Skips anything already cached."""
    rdir = report_dir(code)
    if not (rdir / "report.json").exists():
        log(f"{code}: report")
        report = client.query(_report(code, REPORT_FIELDS))["reportData"]["report"]
        if report["endTime"] > time.time() * 1000 - LIVE_MS:
            log(f"{code}: skipped, it may still be logging; fetch again an hour after it ends")
            return
        _write(rdir / "report.json", report)
    report = read(rdir / "report.json")
    fight_ids = [f["id"] for f in report["fights"] if f["encounterID"] > 0]
    if not fight_ids:
        return
    # Event queries cover only the boss fights
    bounds = f"fightIDs: {fight_ids}"

    if not (rdir / "deaths.json").exists():
        log(f"{code}: deaths")
        _write(rdir / "deaths.json", _paginate_events(client, code, f"dataType: Deaths, {bounds}"))

    if not (rdir / "casts.json").exists():
        log(f"{code}: casts")
        ids = ", ".join(str(a) for a in CAST_ABILITIES)
        expr = f'filterExpression: "ability.id in ({ids})"'
        _write(
            rdir / "casts.json",
            _paginate_events(client, code, f"dataType: Casts, {bounds}, {expr}"),
        )

    if not (rdir / "consumables.json").exists():
        ids = [a["gameID"] for a in report["masterData"]["abilities"] if consumable_kind(a["name"])]
        events = []
        if ids:
            log(f"{code}: consumables")
            expr = f'filterExpression: "ability.id in ({", ".join(map(str, ids))})"'
            events = _paginate_events(client, code, f"dataType: Casts, {bounds}, {expr}")
        _write(rdir / "consumables.json", events)

    if not (rdir / "trash.json").exists():
        log(f"{code}: trash")
        _write(rdir / "trash.json", _fetch_trash(client, code, raid_encounters))

    missing = [f for f in fight_ids if not (rdir / "fights" / f"{f}.json").exists()]
    for i in range(0, len(missing), FIGHTS_PER_REQUEST):
        batch = missing[i : i + FIGHTS_PER_REQUEST]
        log(f"{code}: fights {batch} (points used this hour: {client.points_spent or 0:.0f})")
        fields = []
        for f in batch:
            for key, data_type in FIGHT_TABLES.items():
                fields.append(f"f{f}_{key}: table(dataType: {data_type}, fightIDs: [{f}])")
            fields.append(f"f{f}_players: playerDetails(fightIDs: [{f}])")
        data = client.query(_report(code, " ".join(fields)))["reportData"]["report"]
        for f in batch:
            tables = {key: data[f"f{f}_{key}"]["data"] for key in FIGHT_TABLES}
            tables["players"] = data[f"f{f}_players"]["data"]["playerDetails"]
            _write(rdir / "fights" / f"{f}.json", tables)
