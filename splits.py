#!/usr/bin/env python3
"""
Collect public betting splits into nfl.db and export site/data/splits.json.

Uses the scrapers in nfl_splits.py (DraftKings Network + Scores and Odds),
matches each game to its nflverse game_id, stores a timestamped snapshot in
the `splits` table, and writes the latest snapshot per game for the website.

Run every 30 minutes (see README.md).
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import nfl_splits as ns

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl.db"
OUT = HERE / "site" / "data" / "splits.json"
TO_NFLVERSE = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}


def nv(abbr):
    return TO_NFLVERSE.get(abbr, abbr)


def find_game(con, away, home, ts):
    row = con.execute(
        "SELECT game_id, season, week FROM games WHERE away_team=? AND home_team=? "
        "AND gameday >= date(?, '-1 day') AND gameday <= date(?, '+10 days') "
        "ORDER BY gameday LIMIT 1", (away, home, ts[:10], ts[:10])).fetchone()
    return row


def main():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    games = ns.scrape_dk()
    if not games:
        print("No DraftKings games parsed (layout change?)", file=sys.stderr)
        return 1
    try:
        ns.merge_sao(games, ns.parse_sao(ns.fetch(ns.SAO_URL)))
    except Exception as e:  # noqa: BLE001
        print(f"Scores and Odds skipped: {e}", file=sys.stderr)

    con = sqlite3.connect(DB_PATH)
    latest = {}
    for g in games:
        away, home = nv(ns.team_abbr(g["away"])), nv(ns.team_abbr(g["home"]))
        hit = find_game(con, away, home, ts)
        if not hit:
            print(f"  no nflverse match for {g['away']} @ {g['home']}", file=sys.stderr)
            continue
        gid, season, week = hit
        rows = []
        for s in g["sides"]:
            if s["market"] not in ("Spread", "Total"):
                continue
            market = s["market"].lower()
            sel = s["selection"]
            if market == "total":
                side = "over" if sel.lower().startswith("over") else "under"
            else:
                side = "home" if sel.startswith(g["home"]) else "away"
            row = {"source": s["source"], "market": market, "side": side, "line": s["line"],
                   "odds": s["odds"], "bets": s["bets_pct"], "money": s["handle_pct"]}
            rows.append(row)
            con.execute("INSERT INTO splits VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (ts, season, week, gid, row["source"], market, side, row["line"],
                         row["odds"], row["bets"], row["money"]))
        latest[gid] = {"updated": ts, "rows": rows}
    con.commit()
    con.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(latest))
    print(f"{ts}: saved splits for {len(latest)} games -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
