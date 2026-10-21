"""Command line entry point: `sga <command> <season-slug>`."""

import argparse
import sys

import polars as pl

from sga import bronze, gold, season, silver, slides, suggest
from sga.wcl import WclClient


def log(msg: str) -> None:
    print(msg, flush=True)


def cmd_fetch(args) -> None:
    s = season.load(args.season)
    client = WclClient(log=log)
    zone = bronze.fetch_zone(client, s.zone_id)
    raid_encounters = {e["id"] for e in zone["encounters"]}
    for i, ref in enumerate(s.reports, 1):
        log(f"[{i}/{len(s.reports)}] {ref.code} ({ref.date:%-d %b})")
        bronze.fetch_report(client, ref.code, raid_encounters, log=log)
    log(f"Done. Points used this hour: {client.points_spent or 0:.0f}/{client.limit_per_hour}")


def cmd_build(args) -> None:
    s = season.load(args.season)
    tables = silver.build(s)
    stats = gold.build(s, tables)
    totals = stats["season_totals"].row(0, named=True)
    log(f"Silver: {tables['copy_fights'].height} copies → {tables['pulls'].height} pulls")
    log(f"Gold: {len(stats)} tables in {s.gold_dir}")
    log(", ".join(f"{k}={v}" for k, v in totals.items()))
    for problem in silver.check_report_dates(s, tables) + gold.check_roster(
        gold.make_ctx(s, tables)
    ):
        log(f"Warning: {problem}")


def cmd_slides(args) -> None:
    s = season.load(args.season)
    try:
        order = slides.build(s, log=log)
    except slides.SlideConfigError as e:
        sys.exit(str(e))
    log(f"{len(order)} slides written to {s.gold_dir / 'deck.html'}")


def cmd_suggest_reports(args) -> None:
    s = season.load(args.season)
    df = suggest.candidate_reports(WclClient(log=log), s)
    if df.is_empty():
        log("No unlisted reports found.")
        return
    with pl.Config(tbl_rows=-1, tbl_width_chars=200, fmt_str_lengths=60):
        print(df)


def cmd_suggest_avoidable(args) -> None:
    s = season.load(args.season)
    df = suggest.avoidable_candidates(silver.load(s))
    if args.encounter:
        df = df.filter(pl.col("encounter").str.contains(args.encounter))
    df = df.group_by("encounter", maintain_order=True).head(args.top)
    if args.csv:
        df.write_csv(sys.stdout)
        return
    with pl.Config(tbl_rows=-1, tbl_width_chars=200, fmt_str_lengths=40):
        print(df)


def main() -> None:
    parser = argparse.ArgumentParser(prog="sga", description="Seasonal Guild Aggregator")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("fetch", help="Fetch the season's reports into the bronze cache")
    p.add_argument("season")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("build", help="Build silver tables and gold stats from the cache")
    p.add_argument("season")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("slides", help="Write gold/deck.html from gold and slides.toml")
    p.add_argument("season")
    p.set_defaults(func=cmd_slides)

    p = sub.add_parser("suggest-reports", help="List unlisted reports that may belong")
    p.add_argument("season")
    p.set_defaults(func=cmd_suggest_reports)

    p = sub.add_parser("suggest-avoidable", help="Rank likely avoidable abilities per boss")
    p.add_argument("season")
    p.add_argument("--encounter", help="Filter by encounter name (substring)")
    p.add_argument("--top", type=int, default=8, help="Abilities per encounter")
    p.add_argument("--csv", action="store_true")
    p.set_defaults(func=cmd_suggest_avoidable)

    args = parser.parse_args()
    args.func(args)
