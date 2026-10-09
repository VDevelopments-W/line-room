#!/usr/bin/env python3
"""
Player prop model: the chance a player reaches a line (e.g. 50+ rushing yards).

For each player and stat, using only games before the one being predicted:
  mu   recent production, weighted toward recent games (half-life HL games, carries across seasons)
  sd   recent game-to-game spread around that average
  opp  how much this opponent has allowed to the player's position group vs league average,
       weighted toward recent games and shrunk toward 1.0 until the defense has a real sample
  n    games of history
Then a logistic regression per stat turns (mu, sd, opp, n, home, line) into a calibrated
probability. It is fit on 2012-2022 and tested on 2023-2025 (seasons it never saw).

Outputs (python3 prop_model.py):
  site/data/propmodel.json   coefficients + each active player's current features, so the
                             website can price ANY line (every Kalshi strike), not just streaks
  research/prop_model_test.txt  out-of-sample accuracy and calibration
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "data" / "propmodel.json"
REPORT = HERE / "research" / "prop_model_test.txt"
TRAIN = (2012, 2022)
TEST = (2023, 2025)
HL = 6.0          # player half-life in games
HL_DEF = 8.0      # defense half-life in games
K_DEF = 4.0       # games of shrinkage toward league average for defenses
MIN_N = 2

STATS = {
    "passing_yards":   (("QB",), [150, 175, 200, 225, 250, 275, 300, 325]),
    "passing_tds":     (("QB",), [1, 2, 3]),
    "carries":         (("QB", "RB", "FB", "WR"), [5, 8, 10, 12, 15, 18, 20]),
    "rushing_yards":   (("QB", "RB", "FB", "WR"), [10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 100, 120]),
    "receptions":      (("RB", "FB", "WR", "TE"), [2, 3, 4, 5, 6, 7, 8, 9]),
    "receiving_yards": (("RB", "FB", "WR", "TE"), [10, 15, 20, 25, 30, 40, 50, 60, 70, 80, 100, 120]),
}
SD_FLOOR = {"passing_yards": 45, "passing_tds": 0.7, "carries": 2.5, "rushing_yards": 12,
            "receptions": 1.2, "receiving_yards": 12}
IMP = {}
GROUP = {"QB": "QB", "RB": "RB", "FB": "RB", "WR": "WR", "TE": "TE"}


def load():
    from props import load_players
    df = load_players(refresh_current=False)
    df = df[df.position.isin(GROUP)].copy()
    df["grp"] = df.position.map(GROUP)
    df["order"] = df.season * 100 + df.week
    return df.sort_values(["player_id", "order"]).reset_index(drop=True)


def player_features(df, stat, pos):
    d = df[df.position.isin(pos) & df[stat].notna()][["player_id", "player_display_name", "position", "grp", "team",
                                                      "opponent_team", "season", "week", "order", "game_id", stat]].copy()
    g = d.groupby("player_id", sort=False)[stat]
    alpha = 1 - 0.5 ** (1 / HL)
    d["mu"] = g.transform(lambda x: x.shift().ewm(alpha=alpha, min_periods=1).mean())
    d["m2"] = g.transform(lambda x: (x ** 2).shift().ewm(alpha=alpha, min_periods=1).mean())
    d["n"] = g.cumcount()
    # what each player did last time out, for the "now" features (include the latest game)
    d["mu_now"] = g.transform(lambda x: x.ewm(alpha=alpha).mean())
    d["m2_now"] = g.transform(lambda x: (x ** 2).ewm(alpha=alpha).mean())
    return d


def defense_factor(df, stat):
    """Per defense and position group: allowed / league average, EWMA over prior games, shrunk to 1."""
    pg = df[df[stat].notna()].groupby(["opponent_team", "grp", "season", "week", "order"])[stat].sum().reset_index()
    lg = pg.groupby(["grp", "season", "week"])[stat].transform("mean")
    pg["ratio"] = (pg[stat] / lg.replace(0, np.nan)).fillna(1.0).clip(0.2, 3.0)
    pg = pg.sort_values(["opponent_team", "grp", "order"])
    alpha = 1 - 0.5 ** (1 / HL_DEF)
    gg = pg.groupby(["opponent_team", "grp"], sort=False)["ratio"]
    pg["w_prev"] = gg.transform(lambda x: pd.Series(1.0, index=x.index).shift().fillna(0).ewm(alpha=alpha, adjust=False).mean() / alpha)
    pg["r_prev"] = gg.transform(lambda x: x.shift().ewm(alpha=alpha, min_periods=1).mean()).fillna(1.0)
    pg["f"] = (pg.w_prev * pg.r_prev + K_DEF) / (pg.w_prev + K_DEF)
    pg["w_now"] = gg.transform(lambda x: pd.Series(1.0, index=x.index).ewm(alpha=alpha, adjust=False).mean() / alpha)
    pg["r_now"] = gg.transform(lambda x: x.ewm(alpha=alpha).mean())
    pg["f_now"] = (pg.w_now * pg.r_now + K_DEF) / (pg.w_now + K_DEF)
    return pg[["opponent_team", "grp", "order", "f", "f_now"]]


def design(mu, sd, f, n, home, line, stat, imp):
    """imp = the team's implied points from the closing spread and total (game script)."""
    mu_adj = mu * f
    z = (mu_adj - line) / sd
    li = np.log(np.clip(imp, 8, 45) / 22.5)
    return np.column_stack([z, z * np.abs(z), np.log(f), np.log1p(n), z * np.log1p(n), home,
                            np.log1p(line) - np.log1p(np.maximum(mu_adj, 0)), li, li * z])


def implied_points():
    """game_id -> {team: implied points} from nflverse closing lines (spread_line = expected home margin)."""
    import sqlite3
    con = sqlite3.connect(HERE / "nfl.db")
    out = {}
    for gid, a, h, sl, tl in con.execute("SELECT game_id, away_team, home_team, spread_line, total_line FROM games "
                                         "WHERE spread_line IS NOT NULL AND total_line IS NOT NULL"):
        out[gid] = {h: tl / 2 + sl / 2, a: tl / 2 - sl / 2}
    con.close()
    return out


def make_rows(d, stat, ladder):
    sd = np.sqrt(np.maximum(d.m2 - d.mu ** 2, 0) + SD_FLOOR[stat] ** 2)
    rows = []
    for L in ladder:
        near = (d.mu * 0.2 <= L) & (L <= d.mu * 3 + 2 * sd)
        x = d[near]
        rows.append(pd.DataFrame({"idx": x.index, "line": L, "hit": (x[stat] >= L).astype(int)}))
    r = pd.concat(rows, ignore_index=True)
    dd = d.loc[r.idx]
    X = design(dd.mu.to_numpy(), sd.loc[r.idx].to_numpy(), dd.f.to_numpy(), dd.n.to_numpy(),
               dd.home.to_numpy(), r.line.to_numpy(), stat, dd.imp.to_numpy())
    return X, r.hit.to_numpy(), dd.season.to_numpy(), r


def main():
    df = load()
    # home/away from nflverse game_id: <season>_<wk>_<AWAY>_<HOME>
    df["home"] = (df.game_id.str.split("_").str[3] == df.team).astype(float)
    global IMP
    IMP = implied_points()
    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stats": {}, "players": {}}
    report = [f"Player prop model, out-of-sample test seasons {TEST[0]}-{TEST[1]} (fit on {TRAIN[0]}-{TRAIN[1]})\n"]
    latest_season = int(df.season.max())
    for stat, (pos, ladder) in STATS.items():
        d = player_features(df, stat, pos)
        dfac = defense_factor(df, stat)
        d = d.merge(dfac[["opponent_team", "grp", "order", "f"]], on=["opponent_team", "grp", "order"], how="left")
        d["f"] = d.f.fillna(1.0)
        d = d.merge(df[["player_id", "order", "home"]].drop_duplicates(["player_id", "order"]), on=["player_id", "order"], how="left")
        d["home"] = d.home.fillna(0.5)
        d["imp"] = [IMP.get(g, {}).get(t, 22.5) for g, t in zip(d.game_id, d.team)]
        d["f"] = d.f.clip(0.3, 3.0)
        d = d[(d.n >= MIN_N) & d.mu.notna() & d.m2.notna()].reset_index(drop=True)
        X, y, season, r = make_rows(d, stat, ladder)
        tr = (season >= TRAIN[0]) & (season <= TRAIN[1])
        te = (season >= TEST[0]) & (season <= TEST[1])
        m = LogisticRegression(C=1.0, max_iter=500).fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        base = y[tr].mean()
        brier = np.mean((p - y[te]) ** 2)
        brier0 = np.mean((base - y[te]) ** 2)
        # naive player-only baseline: same features without the logistic calibration
        zte = X[te][:, 0]
        from scipy.stats import norm
        brier_n = np.mean((norm.cdf(zte) - y[te]) ** 2)
        report.append(f"{stat:16} n={te.sum():7d}  Brier model {brier:.4f} | plain normal curve {brier_n:.4f} | coin-flip base {brier0:.4f}")
        bins = np.clip((p * 10).astype(int), 0, 9)
        cal = []
        for b in range(10):
            s = bins == b
            if s.sum() >= 200:
                cal.append(f"   predicted {b*10:2d}-{b*10+9:2d}%: actual {y[te][s].mean()*100:5.1f}%  (n={s.sum()})")
        report.extend(cal)
        # refit on everything through the latest finished season for live use
        allm = season <= latest_season
        m = LogisticRegression(C=1.0, max_iter=500).fit(X[allm], y[allm])
        out["stats"][stat] = {"coef": [round(float(c), 6) for c in m.coef_[0]], "intercept": round(float(m.intercept_[0]), 6),
                              "sd_floor": SD_FLOOR[stat]}
        # current features per active player: latest game this season, "now" values
        cur = player_features(df, stat, pos)
        cur = cur[cur.season == latest_season].groupby("player_id").tail(1)
        sdn = np.sqrt(np.maximum(cur.m2_now - cur.mu_now ** 2, 0) + SD_FLOOR[stat] ** 2)
        for (_, row), s_ in zip(cur.iterrows(), sdn):
            pl = out["players"].setdefault(row.player_display_name, {"id": row.player_id, "team": row.team, "grp": row.grp})
            pl[stat] = [round(float(row.mu_now), 2), round(float(s_), 2), int(row.n) + 1]
        # current defense factors for this stat
        dn = dfac.sort_values("order").groupby(["opponent_team", "grp"]).tail(1)
        out.setdefault("defense", {})[stat] = {f"{r.opponent_team}|{r.grp}": round(float(r.f_now), 3) for r in dn.itertuples()}
        print(report[-1 - len(cal)])
    tr = json.loads((HERE / "site" / "data" / "trends.json").read_text())
    out["implied"] = {}
    for g in tr["week_games"]:
        if g.get("spread_line") is not None and g.get("total_line") is not None:
            out["implied"][g["game_id"]] = {g["home"]: g["total_line"] / 2 + g["spread_line"] / 2, g["away"]: g["total_line"] / 2 - g["spread_line"] / 2}
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(report) + "\n")
    print(f"-> {OUT} ({len(out['players'])} players)")


def prob(model, player, stat, line, opp, home, imp=22.5):
    """Python twin of the website's pricing, for props.py and grading."""
    st = model["stats"].get(stat)
    pl = model["players"].get(player)
    if not st or not pl or stat not in pl:
        return None
    mu, sd, n = pl[stat]
    f = model["defense"][stat].get(f"{opp}|{pl['grp']}", 1.0)
    x = design(np.array([mu]), np.array([sd]), np.array([f]), np.array([n]), np.array([float(home)]), np.array([line]), stat, np.array([imp]))[0]
    z = st["intercept"] + float(np.dot(st["coef"], x))
    return 1 / (1 + np.exp(-z))


if __name__ == "__main__":
    main()
