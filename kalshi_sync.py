#!/usr/bin/env python3
"""
Turn site/data/kalshi.json into one document per game for the Claude-hosted site
(collection "kalshi", doc id = nflverse game id). Keys like "CHI_GB" from a run
without nfl.db are mapped to this week's game ids here.

Usage: python3 kalshi_sync.py [path/to/kalshi.json] -> prints the doc count and writes kalshi_docs.json
"""
import json
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
src = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "site" / "data" / "kalshi.json"
k = json.loads(src.read_text())
con = sqlite3.connect(HERE / "nfl.db")
ids = {f"{a}_{h}": gid for gid, a, h in con.execute(
    "SELECT game_id, away_team, home_team FROM games WHERE result IS NULL AND season=(SELECT MAX(season) FROM games)")}
docs = {}
for key, rows in k["games"].items():
    gid = key if key.count("_") >= 3 else ids.get(key)
    if gid:
        docs[gid] = {"updated": k["generated"], "rows": [{x: r[x] for x in ("player", "stat", "line", "bid", "ask", "last", "volume")} for r in rows]}
(HERE / "kalshi_docs.json").write_text(json.dumps(docs))
print(f"{len(docs)} game docs, {sum(len(d['rows']) for d in docs.values())} prices -> kalshi_docs.json")
