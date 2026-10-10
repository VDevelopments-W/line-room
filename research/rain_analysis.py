#!/usr/bin/env python3
"""
Research only: does rain matter, and is it already in the betting lines?
Needs research/rain_games.csv: each outdoor game's conditions from the NFL gamebook (nflverse play-by-play
"weather" text), classed Dry / Light rain / Rain / Snow / Maybe (forecast wording only, left out).
Writes research/rain_report.txt.

1. Game totals: actual total vs the closing total by rain level (is rain priced in?), and vs our
   model's projection (which knows wind and cold, not rain).
2. Player stats: actual vs the prop model's expectation (player average x defense) by rain level,
   controlling for wind and cold, per stat.
3. Would a rain input improve the prop model? Same out-of-sample test as the other inputs (fit
   2013-2022 outdoor games, test 2023-2025).
"""
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats as sps

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
OUT = HERE / "research" / "rain_report.txt"
LEVELS = [("Dry",), ("Light rain",), ("Rain",), ("Snow",)]


def main():
    rep = []
    w = pd.read_csv(HERE / "research" / "rain_games.csv")
    w = w[w.cond != "Maybe"].copy()
    w["lvl"] = w.cond
    w["rain_f"] = w.cond.isin(["Rain", "Light rain"]).astype(float)
    w["snow_f"] = (w.cond == "Snow").astype(float)
    con = sqlite3.connect(HERE / "nfl.db")
    g = pd.read_sql("SELECT game_id, season, total, total_line, wind, temp FROM games", con)
    con.close()
    d = w.merge(g, on="game_id")
    rep.append(f"RAIN RESEARCH: {len(d)} outdoor games 2012-2025 (conditions at kickoff from the NFL gamebook)\n")
    rep.append("How often: " + ", ".join(f"{k} {v}" for k, v in d.lvl.value_counts().reindex([l[0] for l in LEVELS]).items()) + "\n")

    # 1. totals vs closing line
    rep.append("1. GAME TOTALS vs the closing total (negative = games went under the line)")
    dd = d.dropna(subset=["total_line"])
    for name, *_ in LEVELS:
        x = dd[dd.lvl == name]
        if len(x) < 15:
            continue
        diff = x.total - x.total_line
        under = (diff < 0).sum(); over = (diff > 0).sum()
        p = sps.binomtest(under, under + over, 0.5).pvalue if under + over else 1
        rep.append(f"   {name:28} n={len(x):4d}  avg vs line {diff.mean():+5.1f} pts   unders {under}-{over} ({under / max(under + over, 1) * 100:4.1f}%)  p={p:.2f}")
    # are lines already lower in rain? (market pricing it in)
    rep.append(f"   Closing total, dry {dd[dd.lvl == 'Dry'].total_line.mean():.1f} vs any rain {dd[dd.rain_f == 1].total_line.mean():.1f} "
               "(lower in rain = books already shade it)")
    # regression: total - line on rain, controlling for wind (market sees forecast wind too)
    X = np.column_stack([np.ones(len(dd)), dd.rain_f, dd.snow_f, dd.wind.fillna(dd.wind.mean())])
    b, *_ = np.linalg.lstsq(X, (dd.total - dd.total_line).to_numpy(), rcond=None)
    rep.append(f"   Fit (wind held constant): rain {b[1]:+.1f} pts vs the closing total, snow {b[2]:+.1f}\n")

    # vs our model's projection (walk-forward), which has wind and cold but no rain
    try:
        import model as M
        gg, tg, QB = M.load()
        M.WIND_MEAN = float(gg.loc[~gg.roof.isin(["dome", "closed"]), "wind"].mean())
        df = M.build_rows(gg, tg, QB, list(range(M.FIRST, 2026)), min_week=1)
        bm, bt = M.fit_blend(df[(df.season <= M.FIT_END) & (df.week >= 3)])
        df = M.apply_blend(df, bm, bt).merge(w, on="game_id")
        df = df.dropna(subset=["total"])
        X = np.column_stack([np.ones(len(df)), df.rain_f, df.snow_f])
        b2, *_ = np.linalg.lstsq(X, (df.total - df.total_proj).to_numpy(), rcond=None)
        rep.append(f"   vs OUR MODEL's total (wind and cold in it, no rain): rain {b2[1]:+.1f} pts, snow {b2[2]:+.1f}; "
                   + ", ".join(f"{n.split(' (')[0]} {(df[df.lvl == n].total - df[df.lvl == n].total_proj).mean():+.1f}" for n, *_ in LEVELS if (df.lvl == n).sum() >= 15) + "\n")
    except Exception as e:  # noqa: BLE001
        rep.append(f"   (model comparison skipped: {e})\n")

    # 2. player stats vs expectation
    import prop_model as pm
    pdf, _team = pm.load()
    pdf["home"] = (pdf.game_id.str.split("_").str[3] == pdf.team).astype(float)
    rep.append("2. PLAYER STATS vs what the prop model expected (player average x defense), outdoor games")
    rep.append("   ratio = actual / expected; wind and cold held constant by regression")
    test_rows = {}
    for stat, (pos, ladder) in pm.STATS.items():
        dd2 = pm.player_features(pdf, stat, pos)
        dfac = pm.defense_factor(pdf, stat)
        dd2 = dd2.merge(dfac[["opponent_team", "grp", "order", "f"]], on=["opponent_team", "grp", "order"], how="left")
        dd2["f"] = dd2.f.fillna(1).clip(0.3, 3)
        dd2 = dd2.merge(w[["game_id", "rain_f", "snow_f", "lvl"]], on="game_id", how="inner")
        dd2 = dd2[(dd2.n >= 4) & (dd2.mu > {"passing_yards": 150, "passing_tds": 0.8, "carries": 8, "rushing_yards": 30,
                                            "receptions": 2.5, "receiving_yards": 30}[stat])]
        exp_ = dd2.mu * dd2.f
        ratio = (dd2[stat] / exp_).clip(0, 4)
        X = np.column_stack([np.ones(len(dd2)), dd2.rain_f, dd2.snow_f, dd2.wind_f, dd2.cold_f])
        b, *_ = np.linalg.lstsq(X, ratio.to_numpy(), rcond=None)
        res = ratio - X[:, 3:] @ b[3:]
        by = {n: res[dd2.lvl == n] for n, *_ in LEVELS}
        dry = by["Dry"].mean()
        parts = [f"{n.split(' (')[0]} {(v.mean() / dry - 1) * 100:+5.1f}% (n={len(v)})" for n, v in by.items() if n != "Dry" and len(v) >= 30]
        se = res[dd2.lvl.isin(["Rain", "Light rain"])].std() / max(1, np.sqrt((dd2.rain_f == 1).sum()))
        rep.append(f"   {stat:16} vs dry games: " + "; ".join(parts) + f"   [rain effect {b[1] / dry * 100:+.1f}% ± {1.96 * se / dry * 100:.1f}]")
        test_rows[stat] = (dd2, pos, ladder)
    rep.append("")

    # 3. out-of-sample: does adding rain improve the prop model (Brier), and how much in wet games?
    from sklearn.linear_model import LogisticRegression
    rep.append("3. WOULD IT HELP THE PROP MODEL? Same test as the other inputs: fit 2013-2022, test 2023-2025, outdoor games only")
    pm.IMP = pm.implied_points()
    for stat, (pos, ladder) in pm.STATS.items():
        d = pm.player_features(pdf, stat, pos)
        dfac = pm.defense_factor(pdf, stat)
        d = d.merge(dfac[["opponent_team", "grp", "order", "f"]], on=["opponent_team", "grp", "order"], how="left")
        d["f"] = d.f.fillna(1.0).clip(0.3, 3.0)
        d = d.merge(pdf[["player_id", "order", "home"]].drop_duplicates(["player_id", "order"]), on=["player_id", "order"], how="left")
        d["home"] = d.home.fillna(0.5)
        d["imp"] = [pm.IMP.get(gi, {}).get(t, 22.5) for gi, t in zip(d.game_id, d.team)]
        d = d.merge(w[["game_id", "rain_f", "snow_f"]], on="game_id", how="inner")
        d = d[(d.n >= pm.MIN_N) & d.mu.notna() & d.m2.notna() & (d.season >= 2013)].reset_index(drop=True)
        X, y, season, r = pm.make_rows(d, stat, ladder)
        rain = d.loc[r.idx].rain_f.to_numpy(); snow = d.loc[r.idx].snow_f.to_numpy()
        z = X[:, 0]
        XR = np.column_stack([X, rain, rain * z, snow])
        tr, te = season <= 2022, season >= 2023
        wet = te & (rain + snow > 0)
        out = []
        for name, M_ in (("current", X), ("with rain", XR)):
            m = LogisticRegression(C=1.0, max_iter=1000).fit(M_[tr], y[tr])
            p = m.predict_proba(M_[te])[:, 1]
            pw = m.predict_proba(M_[wet])[:, 1]
            out.append((np.mean((p - y[te]) ** 2), np.mean((pw - y[wet]) ** 2), None))
        all_g = (out[0][0] - out[1][0]) / out[0][0] * 100
        wet_g = (out[0][1] - out[1][1]) / out[0][1] * 100
        rep.append(f"   {stat:16} all outdoor games {all_g:+.2f}% better | rain/snow games (n={wet.sum()} lines) {wet_g:+.2f}% better")
    OUT.write_text("\n".join(rep) + "\n")
    print("\n".join(rep))


if __name__ == "__main__":
    main()
