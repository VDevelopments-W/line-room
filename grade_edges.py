#!/usr/bin/env python3
"""
Grade the edge alerts (data/edges/*.json) and build the report card (site/data/edges_report.json).

Each alert is a paper bet of edges.UNIT dollars at the ilani odds when it was FIRST seen.
  result     win / loss from site/data/boxscores.json; "void" when the player has no box score line
             for a finished game (did not play: sportsbooks refund those)
  clv        Kalshi's closing fair chance (data/kalshi_close) minus the chance ilani's odds implied when we
             flagged it. Positive = the market ended up agreeing the ilani price was too cheap. This is the
             quickest honest sign of a real edge; win/loss needs hundreds of bets to settle down.
  P(profit)  share of 4,000 resamples (by game, since props in one game move together) that are profitable.

Usage: python3 grade_edges.py [--print]
"""
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import edges

HERE = Path(__file__).resolve().parent
D = HERE / "site" / "data"


def payout(odds):
    return odds / 100 if odds > 0 else 100 / -odds


def close_fair(gid, a, cache={}):  # noqa: B006 (per-run cache)
    if gid not in cache:
        f = HERE / "data" / "kalshi_close" / f"{gid}.json"
        cache[gid] = edges.kalshi_book(json.loads(f.read_text())["rows"]) if f.exists() else {}
    m = cache[gid].get((a["player"], a["stat"], a["line"]))
    return None if m is None else (m if a["side"] == "over" else 1 - m)


def p_profit(bets, n=4000):
    g = defaultdict(lambda: [0.0, 0.0])
    for b in bets:
        g[b["gid"]][0] += b["pnl"]
        g[b["gid"]][1] += edges.UNIT
    games = list(g.values())
    if len(games) < 3:
        return None
    rnd, hits = random.Random(7), 0
    for _ in range(n):
        s = [0.0, 0.0]
        for _ in games:
            x = games[rnd.randrange(len(games))]
            s[0] += x[0]; s[1] += x[1]
        hits += s[0] > 0
    return round(hits / n, 3)


def summary(bets):
    done = [b for b in bets if b["result"] in ("win", "loss")]
    if not done:
        return {"bets": 0}
    staked = edges.UNIT * len(done)
    pnl = sum(b["pnl"] for b in done)
    clv = [b["clv"] for b in done if b["clv"] is not None]
    return {"bets": len(done), "wins": sum(b["result"] == "win" for b in done), "staked": staked,
            "profit": round(pnl, 2), "roi": round(pnl / staked, 4),
            "expected_win": round(sum(b["fair"] for b in done) / len(done), 4),
            "win_rate": round(sum(b["result"] == "win" for b in done) / len(done), 4),
            "avg_clv": round(sum(clv) / len(clv), 4) if clv else None,
            "beat_close": round(sum(c > 0 for c in clv) / len(clv), 4) if clv else None,
            "p_profit": p_profit(done), "games": len({b["gid"] for b in done})}


def bucket(h):
    return "under 6h" if h < 6 else "6-24h" if h < 24 else "1-2 days" if h < 48 else "2+ days"


def main():
    box = json.loads((D / "boxscores.json").read_text()).get("games", {})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    bets = []
    for f in sorted((HERE / "data" / "edges").glob("*.json")):
        for a in json.loads(f.read_text()).values():
            first = a["first"]
            b = {"gid": a["gid"], "week": int(a["gid"].split("_")[1]), "label": edges.describe(a), "player": a["player"],
                 "stat": a["stat"], "side": a["side"], "source": a["source"], "start": a["start"],
                 "found": first["t"], "hrs_before": a["hrs_before"], "odds": first["odds"], "fair": first["fair"],
                 "edge": first["edge"], "result": "pending", "actual": None, "pnl": 0.0, "clv": None}
            g = box.get(a["gid"])
            if a["start"] < now and g:
                stats = g.get(a["player"])
                if stats is None or stats.get(a["stat"]) is None:
                    b["result"] = "void"
                else:
                    v = stats[a["stat"]]
                    hit = v >= a["line"]
                    win = hit if a["side"] == "over" else not hit
                    b.update(result="win" if win else "loss", actual=v,
                             pnl=round(edges.UNIT * (payout(first["odds"]) if win else -1), 2))
            cf = close_fair(a["gid"], a)
            if cf is not None:
                b["clv"] = round(cf - edges.implied(first["odds"]), 4)
            bets.append(b)
    groups = lambda f: {k: summary([b for b in bets if f(b) == k]) for k in sorted({f(b) for b in bets})}  # noqa: E731
    rep = {"generated": now, "unit": edges.UNIT, "threshold": edges.THRESH,
           "all": summary(bets), "live": summary([b for b in bets if b["source"] == "live"]),
           "backtest": summary([b for b in bets if b["source"] == "backtest"]),
           "by_week": groups(lambda b: b["week"]), "by_stat": groups(lambda b: b["stat"]),
           "by_side": groups(lambda b: b["side"]), "by_timing": groups(lambda b: bucket(b["hrs_before"])),
           "pending": sum(b["result"] == "pending" for b in bets), "void": sum(b["result"] == "void" for b in bets),
           "bets": sorted(bets, key=lambda b: b["found"], reverse=True)}
    (D / "edges_report.json").write_text(json.dumps(rep, separators=(",", ":")))
    a = rep["all"]
    print(f"edge grading: {a['bets']} graded, {rep['pending']} pending, {rep['void']} void; "
          + (f"ROI {a['roi']:+.1%}, avg CLV {a['avg_clv'] if a['avg_clv'] is None else round(a['avg_clv']*100, 1)} pts, P(profit) {a['p_profit']}" if a["bets"] else ""))
    if "--print" in sys.argv:
        for name in ("backtest", "live"):
            print(f"  {name}: {rep[name]}")
        for k, v in rep["by_week"].items():
            print(f"  week {k}: {v.get('bets')} bets, ROI {v.get('roi')}, CLV {v.get('avg_clv')}")
        for k, v in rep["by_timing"].items():
            print(f"  {k}: {v.get('bets')} bets, ROI {v.get('roi')}, CLV {v.get('avg_clv')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
