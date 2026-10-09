#!/usr/bin/env python3
"""
Line Room power ratings and game projections.

Each week, every team gets an offense and a defense rating for several stats,
fitted on all games played so far (this season weighted most, last season
faded in, regressed toward average early in the year), adjusted for the
strength of every opponent and home field:

    stat(team vs opp) = league avg + offense(team) + defense(opp) + home edge

Stats rated: points, EPA per play, pass yards, rush yards, plays, turnovers.
Projected points per team blend the points rating with the EPA rating (the
blend weights are fit on 2006-2016 and then frozen). The projected spread and
total come from those team points.

Backtest is strictly walk-forward: a game is only ever projected with
ratings built from games played before it.

Usage:  python3 model.py            -> site/data/model.json  (+ backtest report)
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
from qb import QBRatings

USE_QB = True
USE_WX = True        # wind and cold in the projected total (outdoor games)

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl.db"
OUT = HERE / "site" / "data" / "model.json"

FIRST = 2002          # first season with a full prior season of team stats
FIT_END = 2016        # blend weights fit on FIRST..FIT_END, tested after
DECAY = 0.95          # weight per week back within a season (tuned on 2006-2016)
PRIOR_W = 0.6         # weight of last season's games at the start of a season
RIDGE = 1.5           # shrinks team ratings toward 0
STATS = ["points", "epa_play", "pass_yds", "rush_yds", "plays", "turnovers"]


# ----------------------------------------------------------------- data

def load():
    con = sqlite3.connect(DB_PATH)
    g = pd.read_sql("SELECT game_id, season, week, game_type, gameday, away_team, home_team, "
                    "away_score, home_score, result, total, spread_line, total_line, location, "
                    "away_qb_id, home_qb_id, roof, temp, wind "
                    "FROM games", con)
    con.close()
    frames = [pd.read_csv(f, low_memory=False) for f in sorted(glob.glob(str(HERE / "data" / "team" / "t*.csv.gz")))]
    t = pd.concat(frames, ignore_index=True).copy()
    t["team"] = t["team"].replace(FRANCHISE)
    t["plays"] = t["attempts"] + t["carries"] + t["sacks_suffered"]
    t["epa"] = t["passing_epa"].fillna(0) + t["rushing_epa"].fillna(0)
    t["epa_play"] = t["epa"] / t["plays"].where(t["plays"] > 0)
    t["pass_yds"] = t["passing_yards"]
    t["rush_yds"] = t["rushing_yards"]
    t["turnovers"] = t["passing_interceptions"].fillna(0) + t["rushing_fumbles_lost"].fillna(0) \
        + t["sack_fumbles_lost"].fillna(0) + t["receiving_fumbles_lost"].fillna(0)
    t = t[["game_id", "team", "plays", "epa_play", "pass_yds", "rush_yds", "turnovers"]]

    rows = []
    for side, opp in (("home", "away"), ("away", "home")):
        d = g[["game_id", "season", "week", "game_type", "gameday", "location"]].copy()
        d["team"] = g[f"{side}_team"]; d["opp"] = g[f"{opp}_team"]
        d["points"] = g[f"{side}_score"]
        d["qb"] = g[f"{side}_qb_id"]
        d["home"] = np.where(g["location"] == "Neutral", 0.0, 1.0 if side == "home" else -1.0)
        rows.append(d)
    tg = pd.concat(rows, ignore_index=True).merge(t, on=["game_id", "team"], how="left")
    tg["order"] = tg["season"] * 100 + tg["week"]
    QB = QBRatings()
    tg["qbr"] = [QB.rating(q, o)[0] for q, o in zip(tg["qb"], tg["order"])]
    return g, tg, QB


# ----------------------------------------------------------------- ratings

def fit_ratings(hist, teams, weights):
    """Weighted ridge fit of stat = mu + off[team] + def[opp] + hfa*home for each stat."""
    idx = {t: i for i, t in enumerate(teams)}
    n, k = len(hist), len(teams)
    X = np.zeros((n, 2 * k + 2))
    X[:, 0] = 1.0
    ti = hist["team"].map(idx).to_numpy(); oi = hist["opp"].map(idx).to_numpy()
    X[np.arange(n), 1 + ti] = 1.0
    X[np.arange(n), 1 + k + oi] = 1.0
    X[:, -1] = hist["home"].to_numpy()
    w = np.sqrt(weights)
    pen = np.full(2 * k + 2, RIDGE); pen[0] = 0; pen[-1] = 0.5
    out = {}
    for s in STATS:
        y = hist[s].to_numpy(dtype=float)
        ok = ~np.isnan(y)
        Xw = X[ok] * w[ok, None]; yw = y[ok] * w[ok]
        A = Xw.T @ Xw + np.diag(pen)
        beta = np.linalg.solve(A, Xw.T @ yw)
        out[s] = {"mu": beta[0], "hfa": beta[-1],
                  "off": dict(zip(teams, beta[1:1 + k])), "def": dict(zip(teams, beta[1 + k:1 + 2 * k]))}
    return out


def ratings_before(tg, season, week):
    """Ratings using only games before (season, week)."""
    cur = tg[(tg.season == season) & (tg.week < week) & tg.points.notna()]
    prev = tg[(tg.season == season - 1) & tg.points.notna()]
    hist = pd.concat([prev, cur])
    if hist.empty:
        return None
    last_prev_week = prev.week.max() if len(prev) else 0
    # weeks back from "now"; last season's games are faded to PRIOR_W and keep fading as this season goes on
    weeks_back = np.where(hist.season == season, week - hist.week, (last_prev_week - hist.week) + week)
    w = DECAY ** weeks_back
    w = np.where(hist.season == season, w, w * PRIOR_W / DECAY ** 0)
    w = np.where(hist.season == season, w, w * (DECAY ** max(0, week - 1)))
    teams = sorted(set(hist.team) | set(hist.opp))
    r = fit_ratings(hist, teams, w)
    # QB quality behind each team's offensive ratings (same weights as the fit)
    hw = pd.DataFrame({"team": hist.team.to_numpy(), "qbr": hist.qbr.to_numpy(), "w": w})
    hw["wq"] = hw.w * hw.qbr
    agg = hw.groupby("team")[["wq", "w"]].sum()
    r["qb_base"] = (agg.wq / agg.w).to_dict()
    return r


def project(r, team, opp, home):
    p = {}
    for s in STATS:
        m = r[s]
        p[s] = m["mu"] + m["off"].get(team, 0) + m["def"].get(opp, 0) + m["hfa"] * home
    return p


# ----------------------------------------------------------------- backtest

def weather_inputs(x, wx):
    """(wind/10, degrees below 40/10) for outdoor games; game-time history from nflverse, forecast for upcoming."""
    f = (wx or {}).get(x.game_id, {})
    if x.roof in ("dome", "closed") or f.get("roof") in ("dome", "closed"):
        return 0.0, 0.0
    wind = x.wind if pd.notna(x.wind) else f.get("wind")
    temp = x.temp if pd.notna(x.temp) else f.get("temp")
    return (WIND_MEAN if wind is None else wind) / 10, (0 if temp is None else max(0, 40 - temp)) / 10


WIND_MEAN = 9.0


def build_rows(g, tg, QB, seasons, min_week=1, starters=None, wx=None):
    """starters: optional {game_id: {"home": qb_id, "away": qb_id}} for games not played yet.
    wx: optional weather.json games, for forecasts."""
    rows = []
    for season in seasons:
        sg = g[g.season == season]
        for week in sorted(sg.week.unique()):
            if week < min_week:
                continue
            r = ratings_before(tg, season, week)
            if r is None:
                continue
            for _, x in sg[sg.week == week].iterrows():
                hv = 0.0 if x.location == "Neutral" else 1.0
                ph = project(r, x.home_team, x.away_team, hv)
                pa = project(r, x.away_team, x.home_team, -hv)
                order = season * 100 + week
                st = (starters or {}).get(x.game_id, {})
                hq = st.get("home", x.home_qb_id); aq = st.get("away", x.away_qb_id)
                hqr = QB.rating(hq, order)[0]; aqr = QB.rating(aq, order)[0]
                dq_h = hqr - r["qb_base"].get(x.home_team, hqr)
                dq_a = aqr - r["qb_base"].get(x.away_team, aqr)
                rows.append({"game_id": x.game_id, "season": season, "week": week,
                             "home": x.home_team, "away": x.away_team,
                             "h_pts_raw": ph["points"], "a_pts_raw": pa["points"],
                             "h_epa": ph["epa_play"], "a_epa": pa["epa_play"],
                             "h_plays": ph["plays"], "a_plays": pa["plays"],
                             "h_pass": ph["pass_yds"], "a_pass": pa["pass_yds"],
                             "h_rush": ph["rush_yds"], "a_rush": pa["rush_yds"],
                             "h_to": ph["turnovers"], "a_to": pa["turnovers"],
                             "h_qb": hq, "a_qb": aq, "h_qbr": hqr, "a_qbr": aqr, "dq_h": dq_h, "dq_a": dq_a,
                             "result": x.result, "total": x.total,
                             "spread_line": x.spread_line, "total_line": x.total_line,
                             **dict(zip(("wind_f", "cold_f"), weather_inputs(x, wx)))})
    return pd.DataFrame(rows)


def fit_blend(df):
    """margin and total as linear blends of the points and EPA projections."""
    d = df.dropna(subset=["result", "total"])
    Xm = np.column_stack([np.ones(len(d)), d.h_pts_raw - d.a_pts_raw,
                          (d.h_epa - d.a_epa) * (d.h_plays + d.a_plays) / 2, d.a_to - d.h_to,
                          (d.dq_h - d.dq_a) * USE_QB])
    bm = np.linalg.lstsq(Xm, d.result, rcond=None)[0]
    Xt = np.column_stack([np.ones(len(d)), d.h_pts_raw + d.a_pts_raw,
                          (d.h_epa + d.a_epa) * (d.h_plays + d.a_plays) / 2, d.h_plays + d.a_plays,
                          (d.dq_h + d.dq_a) * USE_QB, d.wind_f * USE_WX, d.cold_f * USE_WX])
    bt = np.linalg.lstsq(Xt, d.total, rcond=None)[0]
    return bm, bt


def apply_blend(df, bm, bt):
    df = df.copy()
    df["margin"] = bm[0] + bm[1] * (df.h_pts_raw - df.a_pts_raw) \
        + bm[2] * (df.h_epa - df.a_epa) * (df.h_plays + df.a_plays) / 2 + bm[3] * (df.a_to - df.h_to) \
        + bm[4] * (df.dq_h - df.dq_a) * USE_QB
    df["qb_adj_margin"] = bm[4] * (df.dq_h - df.dq_a) * USE_QB
    df["total_proj"] = bt[0] + bt[1] * (df.h_pts_raw + df.a_pts_raw) \
        + bt[2] * (df.h_epa + df.a_epa) * (df.h_plays + df.a_plays) / 2 + bt[3] * (df.h_plays + df.a_plays) \
        + bt[4] * (df.dq_h + df.dq_a) * USE_QB
    df["wx_adj"] = (bt[5] * df.wind_f + bt[6] * df.cold_f) * USE_WX
    df["total_proj"] = df.total_proj + df.wx_adj
    df["h_pts"] = (df.total_proj + df.margin) / 2
    df["a_pts"] = (df.total_proj - df.margin) / 2
    return df


def evaluate(df):
    d = df.dropna(subset=["result", "spread_line", "total_line"])
    rep = {"games": int(len(d)),
           "mae_margin_model": float((d.margin - d.result).abs().mean()),
           "mae_margin_line": float((d.spread_line - d.result).abs().mean()),
           "mae_total_model": float((d.total_proj - d.total).abs().mean()),
           "mae_total_line": float((d.total_line - d.total).abs().mean())}
    edges = []
    for thr in (1, 2, 3, 4):
        e = d.margin - d.spread_line
        pick = d[e.abs() >= thr]
        side = np.sign((pick.margin - pick.spread_line))
        res = np.sign(pick.result - pick.spread_line) * side
        w, l = int((res > 0).sum()), int((res < 0).sum())
        et = d.total_proj - d.total_line
        pt = d[et.abs() >= thr]
        rt = np.sign(pt.total - pt.total_line) * np.sign(pt.total_proj - pt.total_line)
        tw, tl = int((rt > 0).sum()), int((rt < 0).sum())
        edges.append({"edge": thr, "spread": [w, l], "spread_pct": w / (w + l) if w + l else None,
                      "total": [tw, tl], "total_pct": tw / (tw + tl) if tw + tl else None})
    rep["edges"] = edges
    return rep


# ----------------------------------------------------------------- main

def expected_starters(g, tg, QB):
    """Expected starting QB for each unplayed game this week (depth chart + injury report)."""
    try:
        import injuries as INJ
        season, week, games = INJ.current_week()
        inj, dc = INJ.load(season)
        qbs = INJ.expected_qbs(inj, dc, week)
    except Exception as e:  # noqa: BLE001
        print(f"No injury/depth data, using last starters: {e}")
        return {}
    out = {}
    for gid, away, home in games:
        out[gid] = {s: qbs[t]["id"] for s, t in (("home", home), ("away", away)) if t in qbs}
    return out


def main():
    g, tg, QB = load()
    cur_season = int(g.season.max())
    hist_seasons = list(range(FIRST, cur_season))
    starters = expected_starters(g, tg, QB)
    global WIND_MEAN
    WIND_MEAN = float(g.loc[~g.roof.isin(["dome", "closed"]), "wind"].mean())
    wf = HERE / "site" / "data" / "weather.json"
    wx = json.loads(wf.read_text()).get("games", {}) if wf.exists() else {}
    df = build_rows(g, tg, QB, hist_seasons + [cur_season], min_week=1, starters=starters, wx=wx)
    train = df[(df.season <= FIT_END) & (df.week >= 3)]
    bm, bt = fit_blend(train)
    df = apply_blend(df, bm, bt)

    test = df[(df.season > FIT_END) & (df.season < cur_season) & (df.week >= 3)]
    rep_test = evaluate(test)
    rep_train = evaluate(df[(df.season <= FIT_END) & (df.week >= 3)])
    rep_cur = evaluate(df[(df.season == cur_season)])
    tt = test.dropna(subset=["result"]); big = tt[(tt.dq_h - tt.dq_a).abs() >= 0.05]
    qb_test = {"games": int(len(big)), "mae_model": float((big.margin - big.result).abs().mean()),
               "mae_line": float((big.spread_line - big.result).abs().mean()),
               "mae_noqb": float((big.margin - big.qb_adj_margin - big.result).abs().mean())}

    # this week's projections
    pending = g[(g.season == cur_season) & g.result.isna() & g.spread_line.notna()]
    wk = int(pending.week.min())
    proj = df[(df.season == cur_season) & (df.week == wk)]
    games = []
    for _, x in proj.iterrows():
        games.append({k: (round(float(x[k]), 1) if isinstance(x[k], (float, np.floating)) else x[k])
                      for k in ["game_id", "home", "away", "h_pts", "a_pts", "margin", "total_proj",
                                "h_pass", "a_pass", "h_rush", "a_rush", "h_to", "a_to", "h_epa", "a_epa",
                                "spread_line", "total_line"]})
        games[-1]["h_epa"] = round(float(x.h_epa), 3); games[-1]["a_epa"] = round(float(x.a_epa), 3)
        games[-1].update({"h_qb": QB.name(x.h_qb) or None, "a_qb": QB.name(x.a_qb) or None,
                          "h_qbr": round(float(x.h_qbr), 3), "a_qbr": round(float(x.a_qbr), 3),
                          "qb_adj": round(float(x.qb_adj_margin), 1), "wx_adj": round(float(x.wx_adj), 1)})

    # power ratings table (as of now): points-based net rating per team vs average opponent, neutral field
    r = ratings_before(tg, cur_season, wk)
    teams = sorted(r["points"]["off"].keys())
    power = []
    for t in teams:
        off = r["points"]["off"][t]; de = r["points"]["def"][t]
        power.append({"team": t, "off_pts": round(off, 1), "def_pts": round(de, 1), "net": round(off - de, 1),
                      "off_epa": round(r["epa_play"]["off"][t], 3), "def_epa": round(r["epa_play"]["def"][t], 3),
                      "pass_off": round(r["pass_yds"]["off"][t], 1), "pass_def": round(r["pass_yds"]["def"][t], 1),
                      "rush_off": round(r["rush_yds"]["off"][t], 1), "rush_def": round(r["rush_yds"]["def"][t], 1)})
    power.sort(key=lambda p: -p["net"])
    for i, p in enumerate(power):
        p["rank"] = i + 1

    data = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "season": cur_season, "week": wk, "fit": f"{FIRST}-{FIT_END}", "test": f"{FIT_END + 1}-{cur_season - 1}",
            "backtest_test": rep_test, "backtest_train": rep_train, "current_season": rep_cur,
            "qb_test": qb_test, "games": games, "power": power, "hfa_points": round(r["points"]["hfa"], 2)}
    OUT.write_text(json.dumps(_clean(data), allow_nan=False))

    def show(name, rep):
        print(f"{name}: {rep['games']} games | margin MAE model {rep['mae_margin_model']:.2f} vs line {rep['mae_margin_line']:.2f}"
              f" | total MAE model {rep['mae_total_model']:.2f} vs line {rep['mae_total_line']:.2f}")
        for e in rep["edges"]:
            sp = e["spread_pct"]; tp = e["total_pct"]
            print(f"   edge >= {e['edge']}: spread {e['spread'][0]}-{e['spread'][1]} ({sp and sp * 100:.1f}%)"
                  f"   total {e['total'][0]}-{e['total'][1]} ({tp and tp * 100:.1f}%)")
    show(f"TRAIN {FIRST}-{FIT_END}", rep_train)
    show(f"TEST  {FIT_END + 1}-{cur_season - 1}", rep_test)
    show(f"{cur_season} so far", rep_cur)
    print("Blend margin", np.round(bm, 3), "total", np.round(bt, 3))
    print(f"Week {wk} projections:")
    for x in sorted(games, key=lambda x: x["game_id"]):
        print(f"  {x['away']:>3} {x['a_pts']:5.1f} @ {x['home']:<3} {x['h_pts']:5.1f} | model {x['home']} {-x['margin']:+.1f}  line {-x['spread_line']:+.1f}"
              f" | total {x['total_proj']:.1f} vs {x['total_line']}")
    print("Top 8 power:", [(p["team"], p["net"]) for p in power[:8]])


if __name__ == "__main__":
    main()
