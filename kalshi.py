#!/usr/bin/env python3
"""
Kalshi NFL player prop prices -> site/data/kalshi.json

Kalshi lists player props as "X or more" contracts (e.g. "D'Andre Swift: 50+
rushing yards"). The price in dollars is the market's win probability:
$0.71 = 71%. No API key needed for market data.

Usage:  python3 kalshi.py            (writes site/data/kalshi.json)
        python3 kalshi.py --print    (also prints a summary)

Output: {"generated": ISO, "games": {nflverse_game_id: [row, ...]}}
row = {player, stat, line, bid, ask, last, volume, ticker}
  stat uses the same names as props.py (rushing_yards, receiving_yards,
  receptions, passing_yards, passing_tds, ...); line = the X in "X+".
  bid / ask / last are probabilities 0-1 (None if no quote).
"""
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "data" / "kalshi.json"
DB_PATH = HERE / "nfl.db"
API = "https://api.elections.kalshi.com/trade-api/v2"

# Known per-game prop series. Others matching SERIES_RE are picked up from the series list too.
SERIES = {
    "KXNFLRSHYDS": "rushing_yards",
    "KXNFLRECYDS": "receiving_yards",
    "KXNFLREC": "receptions",
    "KXNFLPASSYDS": "passing_yards",
    "KXNFLPASSTDS": "passing_tds",
    "KXNFLRSHATT": "carries",
    "KXNFLANYTD": "anytime_td",
}
GUESS = [  # title words -> stat, for series discovered at run time
    (r"rush\w*\s+att|carries", "carries"),
    (r"rush\w*\s+y", "rushing_yards"),
    (r"receiving\s+y|rec\w*\s+y", "receiving_yards"),
    (r"reception", "receptions"),
    (r"pass\w*\s+y", "passing_yards"),
    (r"pass\w*\s+t(ouch)?d", "passing_tds"),
    (r"anytime|touchdown scorer", "anytime_td"),
]
TEAM_FIX = {"JAC": "JAX", "LAR": "LA", "WSH": "WAS", "LVR": "LV", "KCC": "KC", "GBP": "GB", "NEP": "NE",
            "NOS": "NO", "SFO": "SF", "TBB": "TB", "ARZ": "ARI", "HST": "HOU", "BLT": "BAL", "CLV": "CLE"}

S = requests.Session()
S.headers["User-Agent"] = "line-room/1.0 (personal NFL dashboard)"


def get(path, **params):
    for attempt in range(4):
        r = S.get(f"{API}{path}", params=params, timeout=30)
        if r.status_code == 429:
            time.sleep(2 + attempt * 2)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    return None


def prob(m, key):
    """Kalshi has used both cents ints (yes_bid: 71) and dollar strings (yes_bid_dollars: "0.7100")."""
    v = m.get(f"{key}_dollars")
    if v not in (None, ""):
        try:
            return round(float(v), 4)
        except ValueError:
            pass
    v = m.get(key)
    if v in (None, ""):
        return None
    v = float(v)
    return round(v / 100, 4) if v > 1 else round(v, 4)


def volume(m):
    for k in ("volume_fp", "volume"):
        if m.get(k) not in (None, ""):
            try:
                return float(m[k])
            except ValueError:
                pass
    return 0.0


def discover_series():
    """Only the plain per-game "X or more" series. Kalshi also runs longest-reception, head-to-head,
    ladder, escalator and season markets that look similar but are different bets."""
    return {k: v for k, v in SERIES.items() if v != "anytime_td"}


def discover_series_all():  # kept for exploring new Kalshi series by hand
    found = dict(SERIES)
    data = get("/series", category="Sports") or {}
    for s in data.get("series", []):
        t, title = s.get("ticker", ""), (s.get("title") or "").lower()
        if not t.startswith("KXNFL") or t in found:
            continue
        if any(w in title for w in ("season", "career", "leader", "week", "mvp", "award", "draft", "coach", "winner", "wins")):
            continue
        for pat, stat in GUESS:
            if re.search(pat, title):
                found[t] = stat
                break
    return found


NFL = {"ARI","ATL","BAL","BUF","CAR","CHI","CIN","CLE","DAL","DEN","DET","GB","HOU","IND","JAX","KC",
       "LA","LAC","LV","MIA","MIN","NE","NO","NYG","NYJ","PHI","PIT","SEA","SF","TB","TEN","WAS"}


def week_games():
    """(away, home) -> nflverse game_id when nfl.db is around (self-host); else keys fall back to AWAY_HOME."""
    if not DB_PATH.exists():
        return {}
    con = sqlite3.connect(DB_PATH)
    rows = con.execute("SELECT game_id, gameday, away_team, home_team FROM games WHERE result IS NULL "
                       "AND season = (SELECT MAX(season) FROM games) AND spread_line IS NOT NULL").fetchall()
    con.close()
    return {(a, h): gid for gid, day, a, h in rows}


def split_teams(code, teams):
    """'CHIGB' -> ('CHI', 'GB') using the known team list."""
    for i in range(2, 4):
        a, h = code[:i], code[i:]
        a, h = TEAM_FIX.get(a, a), TEAM_FIX.get(h, h)
        if a in teams and h in teams:
            return a, h
    return None


def parse_line(m):
    fs = m.get("floor_strike")
    if fs not in (None, ""):
        return int(round(float(fs) + 0.5))
    t = m.get("title") or m.get("yes_sub_title") or ""
    mm = re.search(r"(\d+)\+", t)
    if mm:
        return int(mm.group(1))
    mm = re.search(r"-(\d+)$", m.get("ticker", ""))
    return int(mm.group(1)) if mm else None


def parse_player(m):
    for k in ("title", "yes_sub_title", "subtitle"):
        t = m.get(k) or ""
        if ":" in t:
            return t.split(":")[0].strip()
    return (m.get("yes_sub_title") or "").strip() or None


def fetch():
    games = week_games()
    teams = NFL
    series = discover_series()
    out = {}
    for ticker, stat in series.items():
        cursor = None
        while True:
            data = get("/markets", series_ticker=ticker, status="open", limit=1000, **({"cursor": cursor} if cursor else {}))
            if not data:
                break
            for m in data.get("markets", []):
                ev = m.get("event_ticker", "")
                mm = re.match(r"^[A-Z0-9]+-\d{2}[A-Z]{3}\d{2}([A-Z]+)$", ev)
                if not mm:
                    continue
                pair = split_teams(mm.group(1), teams)
                if not pair:
                    continue
                gid = games.get(pair) or f"{pair[0]}_{pair[1]}"
                player, line = parse_player(m), parse_line(m)
                if not player or line is None:
                    continue
                out.setdefault(gid, []).append({
                    "player": player, "stat": stat, "line": line,
                    "bid": prob(m, "yes_bid"), "ask": prob(m, "yes_ask"), "last": prob(m, "last_price"),
                    "volume": volume(m), "ticker": m.get("ticker")})
            cursor = data.get("cursor")
            if not cursor:
                break
    for rows in out.values():
        rows.sort(key=lambda r: (r["player"], r["stat"], r["line"]))
    return out, series


def main():
    games, series = fetch()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "series": series, "games": games}, indent=0))
    n = sum(len(v) for v in games.values())
    print(f"Kalshi: {n} prop prices across {len(games)} games -> {OUT}")
    if "--print" in sys.argv:
        for gid, rows in sorted(games.items()):
            print(f"  {gid}: {len(rows)} markets, {len({r['player'] for r in rows})} players")


if __name__ == "__main__":
    main()
