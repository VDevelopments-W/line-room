#!/usr/bin/env python3
"""
Referee and head coach records, with an honest persistence test.

Referees: over/under record vs the closing total, average points vs the
total, home team ATS, and penalties per game (team stats, 1999+).
Coaches: ATS overall, as favorite / underdog, home / away, off a bye.

Persistence test (the important part): with 30+ refs and 100+ coaches,
some will look extreme by pure luck. So for every season we ask: if you had
bet each ref's / coach's strong past tendency (from earlier seasons only),
how did those bets do the NEXT season? If that walk-forward record isn't
better than break-even, the past records are noise.

Usage: python3 people.py  ->  site/data/people.json
"""
import glob
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ingest import FRANCHISE
from trends import _clean

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl.db"
OUT = HERE / "site" / "data" / "people.json"
BE = 110 / 210
FIRST_SEASON_LINES = 1999
TEST_FROM = 2008
MIN_PRIOR = 40       # games of history before a tendency counts
LEAN = 0.56          # past rate needed to "bet the tendency"


def load_games():
    con = sqlite3.connect(DB_PATH)
    g = pd.read_sql("SELECT * FROM games WHERE spread_line IS NOT NULL", con)
    con.close()
    g["done"] = g["result"].notna()
    g["over"] = np.sign(g["total"] - g["total_line"])         # 1 over, -1 under, 0 push
    g["home_ats"] = np.sign(g["result"] - g["spread_line"])    # 1 home covered
    g["pts_vs_total"] = g["total"] - g["total_line"]
    return g


def load_penalties():
    frames = []
    for f in sorted(glob.glob(str(HERE / "data" / "team" / "t*.csv.gz"))):
        d = pd.read_csv(f, usecols=["game_id", "team", "penalties", "penalty_yards"])
        frames.append(d)
    t = pd.concat(frames)
    return t.groupby("game_id")[["penalties", "penalty_yards"]].sum()


def rec(sign_series):
    w = int((sign_series > 0).sum()); l = int((sign_series < 0).sum()); p = int((sign_series == 0).sum())
    return w, l, p


def pct(w, l):
    return w / (w + l) if w + l else None


def z_vs_half(w, l):
    n = w + l
    return (w - n / 2) / math.sqrt(n / 4) if n else 0.0


def walk_forward(g, key_col, outcome_col):
    """For each season, a person's tendency from prior seasons -> bet it this season."""
    done = g[g.done & g[key_col].notna()].sort_values(["season", "week"])
    w = l = p = 0
    by_season = []
    for season in range(TEST_FROM, int(done.season.max()) + 1):
        prior = done[done.season < season]
        stats = prior.groupby(key_col)[outcome_col].agg(lambda s: (int((s > 0).sum()), int((s < 0).sum())))
        cur = done[done.season == season]
        sw = sl = 0
        for _, r in cur.iterrows():
            st = stats.get(r[key_col])
            if not st or st[0] + st[1] < MIN_PRIOR:
                continue
            rate = st[0] / (st[0] + st[1])
            if rate >= LEAN:
                side = 1
            elif rate <= 1 - LEAN:
                side = -1
            else:
                continue
            o = r[outcome_col] * side
            if o > 0: sw += 1
            elif o < 0: sl += 1
            else: p += 1
        w += sw; l += sl
        by_season.append({"season": season, "w": sw, "l": sl})
    n = w + l
    return {"w": w, "l": l, "p": p, "pct": pct(w, l),
            "units": round(w * 100 / 110 - l, 1),
            "z": (w - n * BE) / math.sqrt(n * BE * (1 - BE)) if n else None,
            "by_season": by_season}


def referees(g, pen):
    done = g[g.done & g.referee.notna()].copy()
    done = done.join(pen, on="game_id")
    league_pen = done.penalties.mean()
    cur_season = int(g.season.max())
    active = set(g[(g.season >= cur_season - 1) & g.referee.notna()].referee)
    out = []
    for ref, d in done.groupby("referee"):
        if ref not in active or len(d) < 20:
            continue
        ow, ol, op = rec(d.over)
        hw, hl, hp = rec(d.home_ats)
        recent = d[d.season >= cur_season - 3]
        rw, rl, _ = rec(recent.over)
        out.append({
            "name": ref, "games": int(len(d)), "since": int(d.season.min()),
            "over": [ow, ol, op], "over_pct": pct(ow, ol), "over_z": round(z_vs_half(ow, ol), 2),
            "over_recent": [rw, rl], "over_recent_pct": pct(rw, rl),
            "pts_vs_total": round(float(d.pts_vs_total.mean()), 1),
            "home_ats": [hw, hl, hp], "home_ats_pct": pct(hw, hl),
            "penalties": round(float(d.penalties.mean()), 1) if d.penalties.notna().any() else None,
            "penalties_vs_avg": round(float(d.penalties.mean() - league_pen), 1) if d.penalties.notna().any() else None,
        })
    out.sort(key=lambda r: -(r["over_pct"] or 0))
    return out, float(league_pen)


def penalty_persistence(g, pen):
    """Correlation between a ref's penalties/game in prior seasons and the next season."""
    d = g[g.done & g.referee.notna()].join(pen, on="game_id").dropna(subset=["penalties"])
    xs, ys = [], []
    for season in range(TEST_FROM, int(d.season.max()) + 1):
        prior = d[d.season < season].groupby("referee").penalties.agg(["mean", "size"])
        cur = d[d.season == season].groupby("referee").penalties.agg(["mean", "size"])
        lp, lc = d[d.season < season].penalties.mean(), d[d.season == season].penalties.mean()
        for ref in cur.index.intersection(prior.index):
            if prior.loc[ref, "size"] >= MIN_PRIOR and cur.loc[ref, "size"] >= 8:
                xs.append(prior.loc[ref, "mean"] - lp); ys.append(cur.loc[ref, "mean"] - lc)
    return round(float(np.corrcoef(xs, ys)[0, 1]), 2), len(xs)


def coach_rows(g):
    rows = []
    for side, opp in (("home", "away"), ("away", "home")):
        d = g[g.done].copy()
        d["coach"] = d[f"{side}_coach"]
        d["team"] = d[f"{side}_team"]
        sign = 1 if side == "home" else -1
        d["ats"] = sign * d["home_ats"]
        d["fav"] = (sign * d["spread_line"]) > 0
        d["dog"] = (sign * d["spread_line"]) < 0
        d["is_home"] = side == "home"
        d["off_bye"] = d[f"{side}_rest"] >= 13
        d["su"] = np.sign(sign * d["result"])
        rows.append(d[["season", "week", "game_id", "coach", "team", "ats", "fav", "dog", "is_home", "off_bye", "su", "game_type"]])
    return pd.concat(rows)


def coaches(g):
    c = coach_rows(g)
    cur_season = int(g.season.max())
    active = c[c.season == cur_season].groupby("coach")["team"].last().to_dict()
    # current-season coaches also include those whose games have no result yet
    for side in ("home", "away"):
        for _, r in g[(g.season == cur_season)].iterrows():
            active.setdefault(r[f"{side}_coach"], r[f"{side}_team"])
    out = []
    for coach, d in c.groupby("coach"):
        if coach not in active:
            continue
        def r_(mask):
            w, l, p = rec(d[mask].ats)
            return {"w": w, "l": l, "p": p, "pct": pct(w, l)}
        allr = r_(d.ats.notna())
        su_w, su_l, su_p = rec(d.su)
        out.append({
            "name": coach, "team": active[coach], "games": int(len(d)), "since": int(d.season.min()),
            "su": [su_w, su_l, su_p], "ats": allr, "ats_z": round(z_vs_half(allr["w"], allr["l"]), 2),
            "fav": r_(d.fav), "dog": r_(d.dog), "home": r_(d.is_home), "away": r_(~d.is_home),
            "off_bye": r_(d.off_bye), "playoffs": r_(d.game_type != "REG"),
            "recent": r_(d.season >= cur_season - 2),
        })
    out.sort(key=lambda r: -r["ats_z"])
    return out


def coach_walk_forward(g):
    c = coach_rows(g).rename(columns={"ats": "ats_sign"})
    c["done"] = True
    return walk_forward(c, "coach", "ats_sign")


def main():
    g = load_games()
    pen = load_penalties()
    refs, league_pen = referees(g, pen)
    ref_wf = walk_forward(g, "referee", "over")
    ref_home_wf = walk_forward(g, "referee", "home_ats")
    coach_list = coaches(g)
    coach_wf = coach_walk_forward(g)
    pen_r, pen_n = penalty_persistence(g, pen)

    # who's working / coaching this week
    cur = g[g.season == g.season.max()]
    pending = cur[~cur.done]
    wk = int(pending.week.min()) if len(pending) else None
    week = [{"game_id": r.game_id, "away": r.away_team, "home": r.home_team, "referee": r.referee,
             "away_coach": r.away_coach, "home_coach": r.home_coach}
            for _, r in pending[pending.week == wk].iterrows()] if wk else []

    data = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "test_from": TEST_FROM, "min_prior": MIN_PRIOR, "lean": LEAN, "league_penalties": round(league_pen, 1),
            "penalty_persistence": {"r": pen_r, "n": pen_n}, "referees": refs, "ref_over_walk_forward": ref_wf, "ref_home_walk_forward": ref_home_wf,
            "coaches": coach_list, "coach_walk_forward": coach_wf, "week": wk, "week_games": week}
    OUT.write_text(json.dumps(_clean(data), allow_nan=False, default=lambda o: None))

    print(f"Referees: {len(refs)} active")
    for r in refs[:6] + refs[-4:]:
        print(f"  {r['name']:22} {r['games']:4} gms  over {r['over'][0]}-{r['over'][1]} ({r['over_pct']*100:.1f}%) "
              f"z={r['over_z']:+.1f}  last3y {r['over_recent_pct'] and r['over_recent_pct']*100:.0f}%  "
              f"home ATS {r['home_ats_pct']*100:.1f}%  pen {r['penalties']}")
    for name, wf in (("Ref O/U tendency", ref_wf), ("Ref home ATS tendency", ref_home_wf), ("Coach ATS tendency", coach_wf)):
        print(f"WALK-FORWARD {name}: {wf['w']}-{wf['l']}-{wf['p']} ({wf['pct']*100:.1f}%), {wf['units']:+}u, z={wf['z']:.2f}")
    print(f"Penalty persistence r={pen_r} (n={pen_n} ref-seasons)")
    print(f"Coaches: {len(coach_list)} active")
    for c in coach_list[:5] + coach_list[-3:]:
        a = c["ats"]
        print(f"  {c['name']:20} {c['team']:4} ATS {a['w']}-{a['l']}-{a['p']} ({a['pct']*100:.1f}%) z={c['ats_z']:+.1f}  "
              f"dog {c['dog']['w']}-{c['dog']['l']}  fav {c['fav']['w']}-{c['fav']['l']}")


if __name__ == "__main__":
    main()
