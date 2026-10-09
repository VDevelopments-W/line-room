#!/usr/bin/env python3
"""
Collect public betting splits (DraftKings + Scores and Odds) and keep a history.

Runs on GitHub Actions every couple of hours (see .github/workflows/splits.yml).
For every upcoming game this week it:
  - appends a snapshot to data/splits_history/<game_id>.json when anything changed
  - writes the latest snapshot per game to site/data/splits.json
Games that have kicked off are left alone, so their last snapshot is the "close".

Game ids come from site/data/trends.json (written by the daily rebuild), so this
needs no database.

Row shape (same as the website's splits docs):
  {source: dk|sao, market: spread|total, side: home|away|over|under,
   line (DK only), odds (DK only), bets, money}
Usage: python3 collect_splits.py [--dump]
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import nfl_splits as ns

HERE = Path(__file__).resolve().parent
HIST = HERE / "data" / "splits_history"
OUT = HERE / "site" / "data" / "splits.json"
TRENDS = HERE / "site" / "data" / "trends.json"
DEBUG = HERE / "debug"
TO_NFLVERSE = {"LAR": "LA", "JAC": "JAX", "WSH": "WAS"}


def kickoff_utc(g):
    """nflverse gameday/gametime are US Eastern."""
    h, m = map(int, (g.get("gametime") or "13:00").split(":"))
    d = datetime.fromisoformat(g["gameday"]).replace(hour=h, minute=m, tzinfo=ZoneInfo("America/New_York"))
    return d.astimezone(timezone.utc)


def side_rows(g):
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
        rows.append({"source": s["source"], "market": market, "side": side, "line": s["line"],
                     "odds": s["odds"], "bets": s["bets_pct"], "money": s["handle_pct"]})
    return rows


def main():
    dump = "--dump" in sys.argv
    now = datetime.now(timezone.utc)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    tr = json.loads(TRENDS.read_text())
    games = {(g["away"], g["home"]): g for g in tr["season_games"] if g.get("week") == tr["next_week"]}
    games.update({(g["away"], g["home"]): g for g in tr["week_games"]})

    dk = ns.scrape_dk(dump=False)
    if dump or not dk:
        DEBUG.mkdir(exist_ok=True)
        (DEBUG / "dk.html").write_text(ns.fetch(ns.DK_URL, {"tb_eg": ns.NFL_GROUP, "tb_edate": ns.DATE_RANGE, "tb_emt": "0"}), encoding="utf-8")
    sao_n = 0
    try:
        sao_html = ns.fetch(ns.SAO_URL)
        if dump:
            DEBUG.mkdir(exist_ok=True)
            (DEBUG / "sao.html").write_text(sao_html, encoding="utf-8")
        sao = ns.parse_sao(sao_html)
        sao_n = ns.merge_sao(dk, sao)
        # games Scores and Odds has but DraftKings' page doesn't: keep their consensus numbers too
        have = {frozenset((ns.team_abbr(g["away"]), ns.team_abbr(g["home"]))) for g in dk}
        for sg in sao:
            if frozenset(sg["pair"]) in have:
                continue
            a, h = sg["pair"]
            stub = {"event_id": f"sao-{a}-{h}", "away": f"{a} x", "home": f"{h} x", "kickoff": "", "sides": []}
            ns.merge_sao([stub], [sg])
            if stub["sides"]:
                dk.append(stub)
    except Exception as e:  # noqa: BLE001
        print(f"Scores and Odds skipped: {e}", file=sys.stderr)

    HIST.mkdir(parents=True, exist_ok=True)
    latest = json.loads(OUT.read_text()) if OUT.exists() else {}
    saved, unmatched = 0, []
    for g in dk:
        key = (TO_NFLVERSE.get(ns.team_abbr(g["away"]), ns.team_abbr(g["away"])),
               TO_NFLVERSE.get(ns.team_abbr(g["home"]), ns.team_abbr(g["home"])))
        game = games.get(key)
        if not game:
            unmatched.append(f"{g['away']} @ {g['home']}")
            continue
        if kickoff_utc(game) <= now:
            continue  # started: keep the last pre-kickoff snapshot as the close
        gid, rows = game["game_id"], side_rows(g)
        if not rows:
            continue
        f = HIST / f"{gid}.json"
        h = json.loads(f.read_text()) if f.exists() else {"game_id": gid, "kickoff": kickoff_utc(game).strftime("%Y-%m-%dT%H:%M:%SZ"), "snaps": []}
        if not h["snaps"] or h["snaps"][-1]["rows"] != rows:
            h["snaps"].append({"ts": ts, "rows": rows})
            f.write_text(json.dumps(h, separators=(",", ":")))
            saved += 1
        latest[gid] = {"updated": ts, "rows": rows}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(latest, separators=(",", ":")))
    print(f"Splits: {len(dk)} DK games, {sao_n} matched on Scores and Odds, {saved} new snapshots"
          + (f", unmatched: {', '.join(unmatched)}" if unmatched else ""))
    return 0 if dk else 1


if __name__ == "__main__":
    sys.exit(main())
