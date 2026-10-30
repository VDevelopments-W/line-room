#!/usr/bin/env python3
"""
Edge finder: ilani player props priced better than Kalshi says they should be.

Kalshi's NFL prop markets are the "true price" (our tests: more accurate than our own model, and its
closing price was the most accurate number we had). ilani's props come from Kambi and update more
slowly, so when ilani's price implies a lower chance than Kalshi's fair price, that ilani bet is +EV.

Rules (the same ones the Weeks 5-8 backtest used, so live results stay comparable):
  fair      = middle of Kalshi's bid and ask for the same player, stat and line (side-adjusted)
  liquid    = Kalshi ask - bid <= 6 cents and the ask is between 3 and 97 cents
  edge      = fair - ilani's implied chance (no vig removed: ilani's price is what you'd pay)
  alert     = edge >= 3 points, game not started

Every alert is frozen the first time it is seen (time, ilani odds, fair, edge) in
data/edges/<season>_wkNN.json and never rewritten; later runs only update its "last" seen numbers
and whether it is still open. grade_edges.py grades them after the games.

Writes:
  data/edges/<season>_wkNN.json   alert log (append-only first sightings)
  site/data/edges.json            what is open right now, for the site and the Sunday list
  edges_push.txt                  new alerts this run (for the phone notification step), if any

Usage: python3 edges.py [--print]
       functions evaluate()/update_log() are reused by backfill_edges.py on the price history.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
D = HERE / "site" / "data"
LOGDIR = HERE / "data" / "edges"
THRESH = 0.03          # alert when ilani is at least 3 points cheaper than Kalshi's fair chance
MAX_SPREAD = 0.06
UNIT = 20              # paper stake per alert, dollars


def implied(odds):
    return 100 / (odds + 100) if odds > 0 else -odds / (-odds + 100)


def american(p):
    p = min(max(p, 0.01), 0.99)
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def kalshi_book(rows):
    out = {}
    for r in rows or []:
        b, a = r.get("bid"), r.get("ask")
        if b is None or a is None or a - b > MAX_SPREAD or not 0.03 < a < 0.97:
            continue
        out[(r["player"], r["stat"], r["line"])] = (b + a) / 2
    return out


def evaluate(ilani, kalshi, now):
    """All ilani props with a liquid Kalshi match, for games not yet started -> list of dicts."""
    out = []
    kg = kalshi.get("games", {})
    for gid, g in ilani.get("games", {}).items():
        start = g.get("start")
        if not start or datetime.fromisoformat(start.replace("Z", "+00:00")) <= now:
            continue
        a, h = gid.split("_")[2:4]
        book = kalshi_book(kg.get(gid) or kg.get(f"{a}_{h}"))
        if not book:
            continue
        for p in g.get("props", []):
            if p.get("odds") is None:
                continue
            m = book.get((p["player"], p["stat"], p["line"]))
            if m is None:
                continue
            fair = m if p["side"] == "over" else 1 - m
            out.append({"gid": gid, "start": start, "player": p["player"], "stat": p["stat"], "line": p["line"],
                        "side": p["side"], "odds": p["odds"], "fair": round(fair, 4),
                        "edge": round(fair - implied(p["odds"]), 4)})
    return out


def key(x):
    return f"{x['gid']}|{x['player']}|{x['stat']}|{x['line']}|{x['side']}"


def week_file(gid):
    season, wk = gid.split("_")[:2]
    return LOGDIR / f"{season}_wk{wk}.json"


def snap(x, t):
    return {"t": t, "odds": x["odds"], "fair": x["fair"], "edge": x["edge"]}


def update_log(rows, t, logs, source="live"):
    """Fold one snapshot into the logs ({path: {key: alert}}). Returns the alerts that are new."""
    now = datetime.fromisoformat(t.replace("Z", "+00:00"))
    seen, new = set(), []
    for x in rows:
        f = week_file(x["gid"])
        log = logs.setdefault(f, json.loads(f.read_text()) if f.exists() else {})
        k = key(x)
        seen.add((f, k))
        if k in log:
            a = log[k]
            a["last"] = snap(x, t)
            a["open"] = x["edge"] > 0
            if x["edge"] > a["best"]["edge"]:
                a["best"] = snap(x, t)
        elif x["edge"] >= THRESH:
            start = datetime.fromisoformat(x["start"].replace("Z", "+00:00"))
            a = {"gid": x["gid"], "start": x["start"], "player": x["player"], "stat": x["stat"], "line": x["line"],
                 "side": x["side"], "source": x.get("source", source), "hrs_before": round((start - now).total_seconds() / 3600, 1),
                 "first": snap(x, t), "best": snap(x, t), "last": snap(x, t), "open": True}
            log[k] = a
            new.append(a)
    # alerts for games still to play that vanished from ilani or Kalshi this run: closed
    for f, log in logs.items():
        for k, a in log.items():
            if a.get("open") and (f, k) not in seen and datetime.fromisoformat(a["start"].replace("Z", "+00:00")) > now:
                a["open"] = False
    return new


def save(logs):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    for f, log in logs.items():
        f.write_text(json.dumps(log, indent=0, sort_keys=True))


LABEL = {"passing_yards": "Pass Yds", "passing_tds": "Pass TDs", "rushing_yards": "Rush Yds",
         "receiving_yards": "Rec Yds", "receptions": "Receptions", "carries": "Rush Att"}


def describe(a):
    side = f"{a['line']}+" if a["side"] == "over" else f"under {a['line']}"
    return f"{a['player']} {side} {LABEL.get(a['stat'], a['stat'])}"


def injuries():
    try:
        inj = json.loads((D / "injuries.json").read_text())
    except Exception:  # noqa: BLE001
        return {}
    st = {}
    for g in inj.get("games", {}).values():
        for side in ("away", "home"):
            for p in (g.get(side) or {}).get("players", []):
                if p.get("status"):
                    st[p["name"]] = p["status"]
    return st


def main():
    il = json.loads((D / "ilani.json").read_text())
    ka = json.loads((D / "kalshi.json").read_text())
    now = datetime.now(timezone.utc)
    t = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = []
    for name, d in (("ilani", il), ("kalshi", ka)):
        g = datetime.fromisoformat(d["generated"].replace("Z", "+00:00"))
        if now - g > timedelta(minutes=50):
            stale.append(f"{name} is {int((now - g).total_seconds() // 60)} min old")
    rows = [] if stale else evaluate(il, ka, now)
    logs = {}
    new = update_log(rows, t, logs)
    save(logs)

    inj = injuries()
    cur = {key(x): x for x in rows}
    live = []
    for log in logs.values():
        for k, a in log.items():
            if not a.get("open") or k not in cur:
                continue
            x = cur[k]
            live.append({**{c: a[c] for c in ("gid", "start", "player", "stat", "line", "side", "first")},
                         "label": describe(a), "odds": x["odds"], "fair": x["fair"], "edge": x["edge"],
                         "fair_odds": american(x["fair"]), "good_to": american(x["fair"] - 0.01),
                         "status": inj.get(a["player"])})
    live.sort(key=lambda r: -r["edge"])
    soon = [r for r in live if datetime.fromisoformat(r["start"].replace("Z", "+00:00")) - now < timedelta(hours=40)]
    out = {"generated": t, "stale": stale, "threshold": THRESH, "unit": UNIT, "live": live, "trip_list": soon,
           "checked": len(rows)}
    (D / "edges.json").write_text(json.dumps(out, separators=(",", ":")))
    push = HERE / "edges_push.txt"
    if new:
        lines = [f"{describe(a)} at ilani {a['first']['odds']:+d} (fair {american(a['first']['fair']):+d}, edge {a['first']['edge']*100:.1f} pts)"
                 for a in sorted(new, key=lambda a: -a["first"]["edge"])]
        push.write_text("\n".join(lines[:12]) + (f"\n+{len(lines) - 12} more on the Edges tab" if len(lines) > 12 else ""))
    elif push.exists():
        push.unlink()
    print(f"edges: {len(rows)} ilani props matched to Kalshi, {len(live)} open edges, {len(new)} new"
          + (f"; skipped: {', '.join(stale)}" if stale else ""))
    if "--print" in sys.argv:
        for r in live[:25]:
            print(f"  {r['label']:42s} ilani {r['odds']:+5d}  fair {r['fair_odds']:+5d}  edge {r['edge']*100:4.1f}  {r['gid']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
