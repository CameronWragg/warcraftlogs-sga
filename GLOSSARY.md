# Seasonal Guild Aggregator (SGA)

Turns a guild's Warcraft Logs raid reports for one season into season-wide stats for raid leaders and officers to present.

## Language

### Sources

**Report**:
One combat log uploaded to Warcraft Logs by one uploader, identified by its report code.
_Avoid_: Log (when you mean a specific upload), upload

**Duplicate report**:
A report covering the same night as another report. Usually a different uploader, but one person may also split a night across reports.
_Avoid_: Copy (reserve "copy" for a single fight within a report)

**Copy**:
One report's record of a pull. A pull logged by three people has three copies.
_Avoid_: Fight (when you mean the merged pull)

### Raiding

**Season**:
All of a guild's raid nights in one raid tier (currently The Venomous Abyss, from 19 Aug 2026). A season stays open while the tier is current; stats describe it as of the latest night logged.

**Night**:
One raid evening, identified by its local date in the season's timezone. Anything before the cutoff hour (06:00 by default) belongs to the previous evening.
_Avoid_: Session, raid (as a time unit)

**Pull**:
One attempt at a boss encounter in the season's raid, at any raid difficulty, after merging all of its copies. Trash, world content, dungeons and Mythic+ are never pulls.
_Avoid_: Attempt, try, fight

**Trash**:
The non-boss enemies in the season's raid. Never a pull; counted only for damage done to it.

### People

**Player**:
A person in the guild. Every award and attendance figure is per player.
_Avoid_: Raider, member, user

**Character**:
One in-game character, identified by name and server. A player has one or more characters; unless told otherwise, each character is its own player.
_Avoid_: Toon, alt (except to describe a player's non-main character), actor

**Regular**:
A player who attended at least a set share of the season's pulls (10% by default) and, if the season has a roster, is on it. Only regulars are eligible for awards and rate-based leaderboards; everyone counts towards season totals.
_Avoid_: Core, raider

**Filler**:
A player present on guild nights who is not a regular.
_Avoid_: Pug (when they came on a guild night)

### Outcomes

**Kill**:
A pull in which the boss died.

**Wipe**:
A pull that ended without killing the boss.

**Reset**:
A wipe under 30 seconds, most likely the raid resetting a bad pull. Left out of attendance, since Warcraft Logs only lists players who did something in a fight.

**Wipe call**:
The moment the raid gives up on a wipe. Warcraft Logs' recorded call time if it has one, otherwise 10 seconds before the wipe ends. Kills have no wipe call.
_Avoid_: Reset (when you mean the call)

**Avoidable ability**:
An enemy ability a person has judged players could have avoided. Damage from it is avoidable damage. Some count only for players not tanking that pull, such as boss melee.

**Post-call death**:
A death after the wipe call. Excluded from "first to die" and fastest-death stats, but still counted in death totals.

### Deck

**Tie**:
Two or more players (or pulls) whose value looks the same on the slide. Every tied winner is named; the runner-up is the next distinct value.
_Avoid_: Picking one winner of a tie

**Meter**:
A leaderboard slide of the top five regulars by relative DPS or HPS: on each kill, compared with the typical player of the role on that same kill, then weighted by how many kills they played. One row per player per role.
_Avoid_: Parse (a parse is Warcraft Logs' own ranking against other guilds)
