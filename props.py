#!/usr/bin/env python3
"""
Player prop streak board (Linemate-style), with an honest calibration.

For every active player in this week's games it finds:
  - "Last N": the highest standard line the player has cleared in EVERY one
    of his recent games (streak of 5+ games), plus the highest line cleared in
    10+ straight games if that's a different number.
  - "Vs opponent": the highest line cleared in every game against this week's
    opponent (2+ meetings).

Then it attaches what those streaks are actually worth: how often the same
kind of streak (same stat, same streak length, chosen the same way) hit the
NEXT game across 2015-2025, and the break-even odds that implies.

Data: nflverse weekly player stats (free), 2015 to now.
Usage:  python3 props.py   ->  site/data/props.json
"""
import glob
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ingest import FRANCHISE

HERE = Path(__file__).resolve().parent
PLAYER_DIR = HERE / "data" / "players"
URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{y}.csv.gz"
OUT = HERE / "site" / "data" / "props.json"
TRENDS = HERE / "site" / "data" / "trends.json"
CALIB_LAST = 2025

STATS = {
    "passing_yards":   ("Pass Yds",  [150, 175, 200, 225, 250, 275, 300], ("QB",)),
    "passing_tds":     ("Pass TDs",  [1, 2, 3], ("QB",)),
    "carries":         ("Rush Att",  [5, 8, 10, 12, 15, 18, 20], ("QB", "RB", "FB", "WR")),
    "rushing_yards":   ("Rush Yds",  [10, 15, 20, 25, 30, 40, 45, 50, 60, 70, 80, 100], ("QB", "RB", "FB", "WR")),
    "receptions":      ("Rec",       [2, 3, 4, 5, 6, 7, 8], ("RB", "FB", "WR", "TE")),
    "receiving_yards": ("Rec Yds",   [10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 100], ("RB", "FB", "WR", "TE")),
}
LOG_GAMES = 16  # recent games per player kept for same-game parlay correlation
MIN_LAST = 5    # shortest "last games" streak shown
LONG = 10       # second row: line cleared 10+ straight
USE_STACK = True   # vs-opponent adjustment on last-games lines (analysis in research/stack_vs_opponent.py)
MIN_VS = 3      # shortest "vs opponent" streak shown (2-game streaks hit about half the time)


def load_players(refresh_current=True):
    PLAYER_DIR.mkdir(parents=True, exist_ok=True)
    if refresh_current:
        import requests
        y = datetime.now().year
        for yr in (y,) if datetime.now().month >= 8 else (y - 1,):
            r = requests.get(URL.format(y=yr), timeout=120)
            if r.ok:
                (PLAYER_DIR / f"w{yr}.csv.gz").write_bytes(r.content)
    frames = [pd.read_csv(f, low_memory=False) for f in sorted(glob.glob(str(PLAYER_DIR / "w*.csv.gz")))]
    df = pd.concat(frames, ignore_index=True)
    for c in ("team", "opponent_team"):
        df[c] = df[c].replace(FRANCHISE)
    return df.sort_values(["player_id", "season", "week"]).reset_index(drop=True)


def streaks_before(values, ladder):
    """For each game i and ladder threshold t: consecutive games hitting t
    immediately before game i. Returns array [games, len(ladder)]."""
    v = np.asarray(values, dtype=float)
    out = np.zeros((len(v), len(ladder)), dtype=int)
    run = np.zeros(len(ladder), dtype=int)
    for i, x in enumerate(v):
        out[i] = run
        hit = (x >= np.array(ladder)) if not np.isnan(x) else np.zeros(len(ladder), bool)
        run = np.where(hit, run + 1, 0)
    return out


def pick(streak_row, ladder, need):
    """Highest threshold whose streak >= need -> (threshold, streak) or None."""
    ok = np.where(streak_row >= need)[0]
    if not len(ok):
        return None
    j = ok.max()
    return ladder[j], int(streak_row[j])


def stack_status(n_meet, vs_streak):
    """How a last-games streak line looks against this week's opponent."""
    if n_meet == 0:
        return "none"
    if vs_streak >= 3:
        return "stack3"
    if vs_streak >= 2 or vs_streak == n_meet:
        return "stack2"
    return "missed"


def bucket_last(L):
    return "5" if L == 5 else "6-7" if L <= 7 else "8-9" if L <= 9 else "10-14" if L <= 14 else "15+"


def bucket_vs(L):
    return str(L) if L < 5 else "5+"


def calibrate(df):
    """Next-game hit rate for each streak type, chosen exactly as the board chooses."""
    hist = df[df.season <= CALIB_LAST]
    tally = defaultdict(lambda: [0, 0])
    for stat, (_, ladder, pos) in STATS.items():
        h = hist[hist.position.isin(pos) & hist[stat].notna()]
        for pid, g in h.groupby("player_id", sort=False):
            vals = g[stat].to_numpy()
            opps = g["opponent_team"].to_numpy()
            st = streaks_before(vals, ladder)
            lad = np.array(ladder)
            vs_run = defaultdict(lambda: np.zeros(len(ladder), dtype=int))
            vs_n = defaultdict(int)
            for i in range(len(vals)):
                a = pick(st[i], ladder, MIN_LAST)
                if a:
                    t, L = a
                    k = (stat, "last", bucket_last(L))
                    tally[k][0] += vals[i] >= t; tally[k][1] += 1
                    ss = stack_status(vs_n[opps[i]], vs_run[opps[i]][ladder.index(t)])
                    bk = bucket_last(L)
                    for k2 in ((stat, "stack", ss), (stat, "stack", "all"),
                               ("ALL", "stack@" + bk, ss), ("ALL", "stack@" + bk, "all")):
                        tally[k2][0] += vals[i] >= t; tally[k2][1] += 1
                    b = pick(st[i], ladder, LONG)
                    if b and b[0] != t:
                        k = (stat, "last", bucket_last(b[1]))
                        tally[k][0] += vals[i] >= b[0]; tally[k][1] += 1
                hit = (vals[i] >= lad) if not np.isnan(vals[i]) else np.zeros(len(lad), bool)
                vs_run[opps[i]] = np.where(hit, vs_run[opps[i]] + 1, 0)
                vs_n[opps[i]] += 1
            for opp, go in g.groupby("opponent_team", sort=False):
                v2 = go[stat].to_numpy()
                st2 = streaks_before(v2, ladder)
                for i in range(len(v2)):
                    a = pick(st2[i], ladder, MIN_VS)
                    if a:
                        k = (stat, "vs", bucket_vs(a[1]))
                        tally[k][0] += v2[i] >= a[0]; tally[k][1] += 1
    return {k: (h / n if n else None, n) for k, (h, n) in tally.items()}


def american(p):
    if p is None or p <= 0 or p >= 1:
        return None
    return round(-100 * p / (1 - p)) if p >= 0.5 else round(100 * (1 - p) / p)


def board(df, calib):
    tr = json.loads(TRENDS.read_text())
    season, week = tr["season"], tr["next_week"]
    opp_of = {}
    for g in tr["week_games"]:
        opp_of[g["away"]] = (g["home"], g["game_id"], "@")
        opp_of[g["home"]] = (g["away"], g["game_id"], "vs")

    cur = df[df.season == season]
    last_wk = cur.groupby("team")["week"].max().to_dict()
    # active = played in his team's most recent game this season
    latest = cur.sort_values("week").groupby("player_id").tail(1)
    active = latest[latest.apply(lambda r: r.week == last_wk.get(r.team), axis=1)]

    rows = []
    logs = {}
    for _, p in active.iterrows():
        team = p.team
        if team not in opp_of:
            continue
        opp, gid, ha = opp_of[team]
        g = df[df.player_id == p.player_id]
        if team in opp_of:
            last = g.tail(LOG_GAMES)
            logs[p.player_id] = {"games": last["game_id"].tolist(),
                                 **{st: [None if pd.isna(x) else float(x) for x in last[st]] for st in STATS}}
        for stat, (label, ladder, pos) in STATS.items():
            if p.position not in pos:
                continue
            vals = g[stat].to_numpy(dtype=float)
            if len(vals) < MIN_LAST or np.isnan(vals[-MIN_LAST:]).any():
                continue
            st = streaks_before(np.append(vals, np.nan), ladder)[-1]
            season_vals = g[g.season == season][stat].tolist()
            base = {"pid": p.player_id, "player": p.player_display_name, "pos": p.position, "team": team, "opp": opp,
                    "ha": ha, "game_id": gid, "stat": stat, "label": label,
                    "season_avg": round(float(np.mean(season_vals)), 1) if season_vals else None,
                    "recent": [int(x) for x in vals[-5:]], "headshot": p.get("headshot_url")}
            seen = set()
            for need in (MIN_LAST, LONG):
                a = pick(st, ladder, need)
                if not a or a[0] in seen:
                    continue
                seen.add(a[0])
                t, L = a
                hr, n = calib.get((stat, "last", bucket_last(L)), (None, 0))
                gv_all = g[g.opponent_team == opp][stat].to_numpy(dtype=float)
                gv_all = gv_all[~np.isnan(gv_all)]
                run = 0
                for x in gv_all[::-1]:
                    if x >= t: run += 1
                    else: break
                ss = stack_status(len(gv_all), run)
                # stacking effect measured within the same streak-length bucket (all markets pooled)
                s_rate = calib.get(("ALL", "stack@" + bucket_last(L), ss), (None, 0))[0]
                s_all = calib.get(("ALL", "stack@" + bucket_last(L), "all"), (None, 0))[0]
                adj = None if hr is None or s_rate is None or s_all is None else min(0.97, max(0.05, hr + (s_rate - s_all)))
                if not USE_STACK:
                    rows.append(base | {"type": "last", "line": t, "streak": L,
                                        "hit_hist": hr, "n_hist": n, "fair": american(hr)})
                    continue
                rows.append(base | {"type": "last", "line": t, "streak": L, "stack": ss,
                                    "vs_hits": int((gv_all >= t).sum()), "vs_games": int(len(gv_all)), "vs_run": run,
                                    "hit_base": hr, "hit_hist": adj if adj is not None else hr,
                                    "n_hist": n, "fair": american(adj if adj is not None else hr)})
            gv = g[g.opponent_team == opp][stat].to_numpy(dtype=float)
            gv = gv[~np.isnan(gv)]
            if len(gv) >= MIN_VS:
                s2 = streaks_before(np.append(gv, np.nan), ladder)[-1]
                a = pick(s2, ladder, MIN_VS)
                if a:
                    t, L = a
                    hr, n = calib.get((stat, "vs", bucket_vs(L)), (None, 0))
                    rows.append(base | {"type": "vs", "line": t, "streak": L, "games_vs": int(len(gv)),
                                        "hit_hist": hr, "n_hist": n, "fair": american(hr)})
    return season, week, rows, logs


def main():
    df = load_players(refresh_current=False)
    calib = calibrate(df)
    season, week, rows, logs = board(df, calib)
    table = [{"stat": k[0], "type": k[1], "bucket": k[2], "hit": v[0], "n": v[1]}
             for k, v in sorted(calib.items())]
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "season": season, "week": week, "calibration": table, "rows": rows, "logs": logs,
        "labels": {k: v[0] for k, v in STATS.items()},
        "calib_years": f"2015-{CALIB_LAST}"}, allow_nan=False))
    print(f"Week {week}: {len(rows)} streak lines, {len(table)} calibration buckets -> {OUT}")
    for t in table:
        if t["n"] >= 300:
            print(f"  {t['stat']:16} {t['type']:4} {t['bucket']:6} {t['hit'] * 100:5.1f}%  (n={t['n']})")


if __name__ == "__main__":
    main()
