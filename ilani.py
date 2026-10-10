#!/usr/bin/env python3
"""
ilani sportsbook (Kambi) NFL odds -> site/data/ilani.json

ilani's betting site runs on Kambi, whose public odds feed (the same data the betting page loads) is:
  eu-offering-api.kambicdn.com/offering/v2018/ilaniuswarl/...
No login. Polled every 30 minutes on game days by the Kalshi workflow; one list call plus one call per game.

Output: {"generated": ISO, "games": {nflverse_game_id: {
    "event": kambi event id, "start": ISO,
    "spread": [{"side": "home"|"away", "line": -2.5, "odds": -109, "main": true}, ...],   line = that side's spread
    "total":  [{"side": "over"|"under", "line": 43.5, "odds": -112, "main": true}, ...],
    "ml": {"home": -315, "away": 250},
    "props": [{"player", "stat", "line", "side": "over"|"under", "odds"}, ...]
}}}
Props use the same stat names and "line = X in X+" convention as kalshi.json: "250+ Passing Yards" is
line 250 side over; an over/under at 245.5 is line 246 over, and its under is line 246 side under.
Usage: python3 ilani.py [--print]
"""
import json
import math
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import nfl_splits as ns
from collect_splits import TO_NFLVERSE

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "data" / "ilani.json"
BASE = "https://eu-offering-api.kambicdn.com/offering/v2018/ilaniuswarl"
Q = {"lang": "en_US", "market": "US"}
UA = {"User-Agent": "Mozilla/5.0 (line-room personal odds tracker)"}

LADDER = {  # "N+ <label> By The Player - Including Overtime" (outcome "Yes")
    "Passing Yards": "passing_yards", "Touchdown Passes": "passing_tds", "Rush Attempts": "carries",
    "Rushing Yards": "rushing_yards", "Receptions": "receptions", "Receiving Yards": "receiving_yards",
}
OVER_UNDER = {  # "Total <label> by the Player - Including Overtime" (Over / Under at X.5)
    "Total Passing Yards": "passing_yards", "Total Touchdown Passes Thrown": "passing_tds",
    "Total Rushing Attempts": "carries", "Total Rushing Yards": "rushing_yards",
    "Total Receptions": "receptions", "Total Receiving Yards": "receiving_yards",
}


def get(path):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}/{path}", params=Q, headers=UA, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                raise
            print(f"retry {path}: {e}", file=sys.stderr)
            time.sleep(3)


def abbr(kambi_team):
    a = ns.team_abbr(kambi_team)
    return TO_NFLVERSE.get(a, a)


def week_ids():
    tr = json.loads((HERE / "site" / "data" / "trends.json").read_text())
    games = {(g["away"], g["home"]): g["game_id"] for g in tr["season_games"] if g.get("week") == tr["next_week"]}
    games.update({(g["away"], g["home"]): g["game_id"] for g in tr["week_games"]})
    return games


def am(o):
    try:
        return int(o.get("oddsAmerican"))
    except (TypeError, ValueError):
        return None


def parse_event(d, home):
    out = {"spread": [], "total": [], "ml": {}, "props": []}
    for b in d.get("betOffers", []):
        lab = b["criterion"].get("englishLabel") or b["criterion"]["label"]
        main = "MAIN_LINE" in b.get("tags", []) or "MAIN" in b.get("tags", [])
        outs = [o for o in b["outcomes"] if o.get("status") == "OPEN"]
        if lab.startswith("Point Spread - Including"):
            for o in outs:
                side = "home" if abbr(o.get("participant", "")) == home else "away"
                out["spread"].append({"side": side, "line": o["line"] / 1000, "odds": am(o), "main": main})
        elif lab.startswith("Total Points - Including"):
            for o in outs:
                out["total"].append({"side": "over" if o["type"] == "OT_OVER" else "under", "line": o["line"] / 1000, "odds": am(o), "main": main})
        elif lab.startswith("Moneyline - Including"):
            for o in outs:
                out["ml"]["home" if abbr(o.get("participant", "")) == home else "away"] = am(o)
        elif lab.endswith("By The Player - Including Overtime"):
            m = re.match(r"(\d+)\+ (.+) By The Player", lab)
            stat = m and LADDER.get(m.group(2))
            if stat:
                for o in outs:
                    if o.get("label") == "Yes" and o.get("participant"):
                        out["props"].append({"player": o["participant"], "stat": stat, "line": int(m.group(1)), "side": "over", "odds": am(o)})
        elif lab.endswith("by the Player - Including Overtime"):
            stat = OVER_UNDER.get(lab.replace(" by the Player - Including Overtime", ""))
            if stat:
                for o in outs:
                    if o.get("participant") and o.get("line") is not None:
                        out["props"].append({"player": o["participant"], "stat": stat, "line": math.ceil(o["line"] / 1000),
                                             "side": "over" if o.get("label") == "Over" else "under", "odds": am(o), "ou": o["line"] / 1000})
    return out


def main():
    ids = week_ids()
    lv = get("listView/american_football/nfl/all/all/matches.json")
    now = datetime.now(timezone.utc)
    games, unmatched = {}, []
    for e in lv.get("events", []):
        ev = e["event"]
        if " @ " not in ev.get("name", ""):
            continue
        start = datetime.fromisoformat(ev["start"].replace("Z", "+00:00"))
        if start < now - timedelta(hours=4) or start > now + timedelta(days=8):
            continue
        a, h = [t.strip() for t in ev["name"].split(" @ ", 1)]
        gid = ids.get((abbr(a), abbr(h)))
        if not gid:
            unmatched.append(ev["name"])
            continue
        if start <= now:
            continue  # started: leave the pre-kickoff prices in place
        g = parse_event(get(f"betoffer/event/{ev['id']}.json"), abbr(h))
        games[gid] = {"event": ev["id"], "start": ev["start"], **g}
        time.sleep(0.3)
    old = json.loads(OUT.read_text()).get("games", {}) if OUT.exists() else {}
    for gid, g in old.items():  # keep last prices for games that have kicked off (closing number)
        if gid not in games and gid in ids.values():
            games[gid] = g
    OUT.write_text(json.dumps({"generated": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "games": games}, separators=(",", ":")))
    n = sum(len(g["props"]) for g in games.values())
    print(f"ilani: {len(games)} games, {n} player prop prices -> {OUT}" + (f"; unmatched: {', '.join(unmatched)}" if unmatched else ""))
    if "--print" in sys.argv:
        for gid, g in sorted(games.items()):
            ms = [s for s in g["spread"] if s["main"] and s["side"] == "home"]
            print(f"  {gid}: home spread {ms[0]['line'] if ms else '?'} ({ms[0]['odds'] if ms else ''}), {len(g['props'])} props")
    return 0 if games else 1


if __name__ == "__main__":
    sys.exit(main())
