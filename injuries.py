#!/usr/bin/env python3
"""
Injury reports and expected starters.

Sources (nflverse, free, updated through the week):
  injuries_{season}.csv     official practice and game-status reports
  depth_charts_{season}.csv daily depth-chart snapshots

expected_qbs(): this week's starting QB per team = depth chart QB1, unless the
injury report lists him Out or Doubtful, then the next QB on the chart.

Usage: python3 injuries.py [--refresh]  ->  site/data/injuries.json
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DIR = HERE / "data" / "injuries"
DB_PATH = HERE / "nfl.db"
OUT = HERE / "site" / "data" / "injuries.json"
BASE = "https://github.com/nflverse/nflverse-data/releases/download"
OUT_STATUSES = {"Out", "Doubtful"}
KEY_POS = {"QB", "RB", "WR", "TE", "LT", "LG", "C", "RG", "RT", "LDE", "RDE", "LDT", "RDT", "NT",
           "WLB", "MLB", "SLB", "LILB", "RILB", "LCB", "RCB", "SS", "FS", "NB", "PK"}


def refresh(season):
    import requests
    DIR.mkdir(parents=True, exist_ok=True)
    for name in (f"injuries/injuries_{season}.csv", f"depth_charts/depth_charts_{season}.csv"):
        r = requests.get(f"{BASE}/{name}", timeout=180)
        r.raise_for_status()
        (DIR / name.split("/")[1]).write_bytes(r.content)


def current_week():
    con = sqlite3.connect(DB_PATH)
    season, week = con.execute("SELECT season, MIN(week) FROM games WHERE result IS NULL AND "
                               "spread_line IS NOT NULL AND season = (SELECT MAX(season) FROM games)").fetchone()
    games = con.execute("SELECT game_id, away_team, home_team FROM games WHERE season=? AND week=?",
                        (season, week)).fetchall()
    con.close()
    return season, week, games


def load(season):
    inj = pd.read_csv(DIR / f"injuries_{season}.csv", low_memory=False)
    dc = pd.read_csv(DIR / f"depth_charts_{season}.csv", low_memory=False)
    dc = dc[dc.dt == dc.groupby("team").dt.transform("max")]
    dc["team"] = dc["team"].replace({"LAR": "LA", "JAC": "JAX", "WSH": "WAS"})
    inj["team"] = inj["team"].replace({"LAR": "LA", "JAC": "JAX", "WSH": "WAS"})
    return inj, dc


def week_report(inj, week):
    w = inj[(inj.week == week) & inj.report_status.notna()]
    return w


def starters_set(dc):
    """gsis ids of players listed first at a key position on their team's chart."""
    first = dc[(dc.pos_rank == 1) & dc.pos_abb.isin(KEY_POS)]
    return set(first.gsis_id.dropna())


def expected_qbs(inj, dc, week):
    rep = week_report(inj, week)
    out_ids = set(rep[rep.report_status.isin(OUT_STATUSES)].gsis_id)
    qbs = dc[dc.pos_abb == "QB"].sort_values(["team", "pos_rank"])
    res = {}
    for team, d in qbs.groupby("team"):
        rows = d.to_dict("records")
        if not rows:
            continue
        starter = next((r for r in rows if r["gsis_id"] not in out_ids), rows[0])
        res[team] = {"id": starter["gsis_id"], "name": starter["player_name"],
                     "qb1": rows[0]["player_name"], "qb1_out": rows[0]["gsis_id"] in out_ids}
    return res


def main():
    season, week, games = current_week()
    if "--refresh" in sys.argv:
        refresh(season)
    inj, dc = load(season)
    rep = week_report(inj, week)
    starters = starters_set(dc)
    qbs = expected_qbs(inj, dc, week)
    out = {}
    for gid, away, home in games:
        g = {}
        for side, team in (("away", away), ("home", home)):
            r = rep[rep.team == team]
            players = [{"name": x.full_name, "pos": x.position, "status": x.report_status,
                        "injury": x.report_primary_injury if isinstance(x.report_primary_injury, str) else "",
                        "starter": x.gsis_id in starters}
                       for x in r.itertuples()]
            order = {"Out": 0, "Doubtful": 1, "Questionable": 2}
            players.sort(key=lambda p: (order.get(p["status"], 3), not p["starter"], p["pos"]))
            g[side] = {"team": team, "qb": qbs.get(team), "players": players}
        out[gid] = g
    stamp = inj.get("date_modified")
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "season": season, "week": week, "games": out}))
    n_out = sum(1 for g in out.values() for s in g.values() for p in s["players"] if p["status"] in OUT_STATUSES)
    print(f"Week {week}: {len(out)} games, {n_out} players Out/Doubtful -> {OUT}")
    for g in out.values():
        for s in g.values():
            q = s["qb"]
            if q and q["qb1_out"]:
                print(f"  QB change: {s['team']} {q['qb1']} out -> {q['name']}")


if __name__ == "__main__":
    main()
