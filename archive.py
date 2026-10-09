#!/usr/bin/env python3
"""
Freeze this week's picks before kickoff so they can be graded honestly later.

Runs in the daily rebuild after model.py / props.py / prop_model.py. For every game that
has NOT kicked off yet it (re)writes that game's entries; games that already started keep
whatever was saved before kickoff. Files: data/archive/<season>_wk<NN>/
  model.json        model projection per game (margin, total, the line at the time)
  streaks.json      the prop streak board rows (with pooled history hit rate)
  prop_prices.json  every Kalshi line for the game with the model's probability and the
                    Kalshi price at archive time (the closing price comes from data/kalshi_close)
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import prop_model as pm

HERE = Path(__file__).resolve().parent
D = HERE / "site" / "data"


def kickoff(g):
    h, m = map(int, (g.get("gametime") or "13:00").split(":"))
    return datetime.fromisoformat(g["gameday"]).replace(hour=h, minute=m, tzinfo=ZoneInfo("America/New_York"))


def merge(path, fresh, open_ids):
    """Keep saved entries for games already underway; replace entries for games still to come."""
    old = json.loads(path.read_text()) if path.exists() else {}
    keep = {gid: v for gid, v in old.items() if gid not in open_ids}
    keep.update({gid: v for gid, v in fresh.items() if gid in open_ids})
    path.write_text(json.dumps(keep, separators=(",", ":")))
    return len(keep)


def main():
    tr = json.loads((D / "trends.json").read_text())
    season, week = tr["season"], tr["next_week"]
    now = datetime.now(timezone.utc)
    games = {g["game_id"]: g for g in tr["week_games"]}
    open_ids = {gid for gid, g in games.items() if kickoff(g) > now}
    out = HERE / "data" / "archive" / f"{season}_wk{week:02d}"
    out.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    model = json.loads((D / "model.json").read_text())
    mg = {g["game_id"]: {**{k: g.get(k) for k in ("margin", "total_proj", "h_pts", "a_pts", "h_qb", "a_qb", "qb_adj")},
                         "spread_line": games.get(g["game_id"], {}).get("spread_line"),
                         "total_line": games.get(g["game_id"], {}).get("total_line"), "saved": stamp}
          for g in model.get("games", [])}
    n1 = merge(out / "model.json", mg, open_ids)

    props = json.loads((D / "props.json").read_text())
    st = {}
    for r in props["rows"]:
        st.setdefault(r["game_id"], []).append({k: r.get(k) for k in ("player", "team", "opp", "stat", "line", "type", "streak",
                                                                        "stack", "hit_hist", "p_model")})
    n2 = merge(out / "streaks.json", st, open_ids)

    pp = {}
    kpath = D / "kalshi.json"
    if kpath.exists():
        model_p = json.loads((D / "propmodel.json").read_text())
        k = json.loads(kpath.read_text())
        for key, rows in k.get("games", {}).items():
            gid = key if key in games else next((x for x, g in games.items() if f"{g['away']}_{g['home']}" == key), None)
            if not gid:
                continue
            g = games[gid]
            imp = model_p.get("implied", {}).get(gid, {})
            for r in rows:
                pl = model_p["players"].get(r["player"])
                if not pl:
                    continue
                team = pl["team"]
                opp = g["home"] if team == g["away"] else g["away"]
                p = pm.prob(model_p, r["player"], r["stat"], r["line"], opp, team == g["home"], imp.get(team, 22.5), gid)
                if p is None:
                    continue
                pp.setdefault(gid, []).append({"player": r["player"], "stat": r["stat"], "line": r["line"], "p": round(float(p), 4),
                                               "ask": r["ask"], "bid": r["bid"], "volume": r["volume"], "saved": stamp})
    n3 = merge(out / "prop_prices.json", pp, open_ids)
    print(f"Archive {out.name}: model {n1} games, streak board {n2} games, priced props {n3} games ({len(open_ids)} still to play)")


if __name__ == "__main__":
    main()
