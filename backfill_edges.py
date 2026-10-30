#!/usr/bin/env python3
"""
One-time backfill: replay saved ilani + Kalshi price history through edges.py's rules, so the alert log
starts with Weeks 5-8 (marked source "backtest"). Each ilani snapshot is paired with the Kalshi snapshot
closest in time (within 45 minutes), exactly as the live job would have seen them.

Usage: python3 backfill_edges.py <dir of ilani.json versions> <dir of kalshi.json versions>
  (get them with: git log --format=%h -- site/data/ilani.json | while read h; do git show $h:site/data/ilani.json > dir/$h.json; done)
"""
import glob
import json
import sys
from datetime import datetime

import edges


def load(d):
    out = []
    for f in glob.glob(f"{d}/*.json"):
        try:
            j = json.load(open(f))
        except Exception:  # noqa: BLE001
            continue
        if j.get("generated"):
            out.append((datetime.fromisoformat(j["generated"].replace("Z", "+00:00")), j))
    return sorted(out, key=lambda x: x[0])


NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def main():
    il, ka = load(sys.argv[1]), load(sys.argv[2])
    logs, n = {}, 0
    for t, j in il:
        k = min(ka, key=lambda x: abs((x[0] - t).total_seconds()))
        if abs((k[0] - t).total_seconds()) > 45 * 60:
            continue
        rows = edges.evaluate(j, k[1], t)
        for r in rows:  # games already played are the backtest; anything still to play counts as live
            r["source"] = "backtest" if r["start"] < NOW else "live"
        n += len(edges.update_log(rows, t.strftime("%Y-%m-%dT%H:%M:%SZ"), logs))
    # the backfill only covers games already played; do not leave anything open
    for log in logs.values():
        for a in log.values():
            if a["start"] < NOW:
                a["open"] = False
    edges.save(logs)
    print(f"backfill: {n} alerts across {len(logs)} weeks")


if __name__ == "__main__":
    main()
