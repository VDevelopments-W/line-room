#!/usr/bin/env python3
"""
Player prop model: the chance a player reaches a line (e.g. 50+ rushing yards).

For each player and stat, using only games before the one being predicted:
  mu   recent production, weighted toward recent games (half-life HL games, carries across seasons)
  sd   recent game-to-game spread around that average
  opp  how much this opponent has allowed to the player's position group vs league average,
       weighted toward recent games and shrunk toward 1.0 until the defense has a real sample
  n    games of history
  usage   target share (catches/yards) or carry share (rushing), recent 2-game trend vs longer run,
          and offensive snap % trend: a role that is growing or shrinking shows here before the stats
  out     share of the team's targets/carries left behind by teammates who played last game and
          aren't playing this one (live: listed Out or Doubtful). Same-position share counted separately.
  weather wind and cold at kickoff (outdoor games only)
  formula the "team completions x yards per completion the defense allows x target share" projection
          (receiving), which adds a little on top of the rest in testing
Then a logistic regression per stat turns all of that plus the line into a calibrated
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


A_L, A_S = 1 - 0.5 ** (1 / HL), 1 - 0.5 ** (1 / 2.0)   # long (6 games) and short (2 games) usage averages
SHARE = {"receptions": "tsh", "receiving_yards": "tsh", "carries": "csh", "rushing_yards": "csh"}
N_EXTRA = 8


def _ewm(g, c, alpha, shift):
    return g[c].transform(lambda x: (x.shift() if shift else x).ewm(alpha=alpha, min_periods=1).mean())


def load():
    """Weekly player rows with usage, snaps, teammates-out, weather and team passing context attached."""
    import glob
    import sqlite3
    from props import load_players
    raw = load_players(refresh_current=False)
    raw["order"] = raw.season * 100 + raw.week
    tt = raw.groupby(["game_id", "team"]).agg(team_tgt=("targets", "sum"), team_car=("carries", "sum")).reset_index()
    raw = raw.merge(tt, on=["game_id", "team"], how="left")
    raw["tsh"] = (raw.targets.fillna(0) / raw.team_tgt.replace(0, np.nan)).fillna(0)
    raw["csh"] = (raw.carries.fillna(0) / raw.team_car.replace(0, np.nan)).fillna(0)
    files = [f for f in glob.glob(str(HERE / "data" / "snaps" / "s*.csv.gz"))]
    if files:
        sn = pd.concat([pd.read_csv(f, usecols=["game_id", "player", "offense_pct"]) for f in files], ignore_index=True)
        sn = sn.drop_duplicates(["game_id", "player"]).rename(columns={"player": "player_display_name", "offense_pct": "snap"})
        raw = raw.merge(sn, on=["game_id", "player_display_name"], how="left")
    else:
        raw["snap"] = np.nan
    raw = raw.sort_values(["player_id", "order"]).reset_index(drop=True)
    g = raw.groupby("player_id", sort=False)
    for c in ("tsh", "csh", "snap"):
        raw[c + "_l"] = _ewm(g, c, A_L, True)
        raw[c + "_s"] = _ewm(g, c, A_S, True)
        raw[c + "_ln"] = _ewm(g, c, A_L, False)   # "now" (after this game), for live use
        raw[c + "_sn"] = _ewm(g, c, A_S, False)

    # teammates out: played the team's previous game, not in this one
    tg = raw[["team", "season", "order", "game_id"]].drop_duplicates(["team", "game_id"]).sort_values(["team", "order"])
    tg["prev_gid"] = tg.groupby(["team", "season"]).game_id.shift()
    present = raw.groupby(["game_id", "team"]).player_id.apply(set).to_dict()
    prev = {k: v for k, v in raw[["game_id", "team", "player_id", "position", "tsh_ln", "csh_ln"]].groupby(["game_id", "team"])}
    vac = []
    for r in tg.itertuples():
        p = prev.get((r.prev_gid, r.team)) if isinstance(r.prev_gid, str) else None
        if p is None:
            continue
        gone = p[~p.player_id.isin(present.get((r.game_id, r.team), set()))]
        gg = gone.assign(grp=gone.position.map(GROUP)).groupby("grp")[["tsh_ln", "csh_ln"]].sum()
        for grp in ("QB", "RB", "WR", "TE"):
            vt, vc = (gg.loc[grp].tolist() if grp in gg.index else (0.0, 0.0))
            vac.append((r.game_id, r.team, grp, vt, vc))
    vac = pd.DataFrame(vac, columns=["game_id", "team", "grp", "vt_same", "vc_same"])
    va = vac.groupby(["game_id", "team"])[["vt_same", "vc_same"]].sum().rename(columns={"vt_same": "vt_all", "vc_same": "vc_all"}).reset_index()

    # team passing context for the formula: completions per game (team) and receiving yards per completion allowed (defense)
    team = raw.groupby(["game_id", "team", "opponent_team", "order"]).agg(cmp=("completions", "sum"), ryds=("receiving_yards", "sum")).reset_index()
    team = team.sort_values("order")
    team["ypc"] = team.ryds / team.cmp.replace(0, np.nan)
    gt, go = team.groupby("team", sort=False), team.groupby("opponent_team", sort=False)
    a_d = 1 - 0.5 ** (1 / HL_DEF)
    team["cmp_l"], team["cmp_ln"] = _ewm(gt, "cmp", A_L, True), _ewm(gt, "cmp", A_L, False)
    team["dypc_l"], team["dypc_ln"] = _ewm(go, "ypc", a_d, True), _ewm(go, "ypc", a_d, False)

    # weather (nflverse records game-time wind and temperature for outdoor games)
    con = sqlite3.connect(HERE / "nfl.db")
    gm = pd.read_sql("SELECT game_id, roof, temp, wind FROM games", con)
    con.close()
    outdoor = ~gm.roof.isin(["dome", "closed"])
    global WIND_MEAN
    WIND_MEAN = float(gm.loc[outdoor, "wind"].mean())
    gm["wind_f"] = np.where(outdoor, gm.wind.fillna(WIND_MEAN), 0) / 10
    gm["cold_f"] = np.where(outdoor, np.maximum(0, 40 - gm.temp.fillna(60)), 0) / 10

    df = raw[raw.position.isin(GROUP)].copy()
    df["grp"] = df.position.map(GROUP)
    df = df.merge(va, on=["game_id", "team"], how="left").merge(vac, on=["game_id", "team", "grp"], how="left")
    df = df.merge(team[["game_id", "team", "cmp_l"]], on=["game_id", "team"], how="left")
    df = df.merge(team[["game_id", "opponent_team", "dypc_l"]], on=["game_id", "opponent_team"], how="left")
    df = df.merge(gm[["game_id", "wind_f", "cold_f"]], on="game_id", how="left")
    for c in ("vt_all", "vc_all", "vt_same", "vc_same", "wind_f", "cold_f"):
        df[c] = df[c].fillna(0)
    return df.sort_values(["player_id", "order"]).reset_index(drop=True), team


WIND_MEAN = 9.0


def _lt(a, b):
    """log ratio of a short-run usage average to the long-run one (0 = steady role)."""
    return np.log((np.nan_to_num(np.asarray(a, float)) + 0.03) / (np.nan_to_num(np.asarray(b, float)) + 0.03))


def extras(stat, z, sd, line, sh_s, sh_l, snap_s, snap_l, v_all, v_same, wind, cold, cmp_, dypc, tsh):
    """The 8 added model inputs, in a fixed order (the website computes the same thing)."""
    n = len(z)
    sh = SHARE.get(stat)
    trend = _lt(sh_s, sh_l) if sh else np.zeros(n)
    snap = _lt(snap_s, snap_l)
    boost = np.log(1 / (1 - np.clip(np.nan_to_num(np.asarray(v_all, float)), 0, 0.7)))
    vsame = np.nan_to_num(np.asarray(v_same, float)) if sh else np.zeros(n)
    if stat in ("receiving_yards", "receptions"):
        proj = np.nan_to_num(np.asarray(cmp_, float)) * np.nan_to_num(np.asarray(tsh, float))
        if stat == "receiving_yards":
            proj = proj * np.nan_to_num(np.asarray(dypc, float), nan=11.0)
        zf = (proj - line) / sd
    else:
        zf = np.zeros(n)
    return np.column_stack([trend, trend * z, snap, boost, vsame, np.asarray(wind, float), np.asarray(cold, float), zf])


CTX = ["tsh_l", "tsh_s", "csh_l", "csh_s", "snap_l", "snap_s", "tsh_ln", "tsh_sn", "csh_ln", "csh_sn", "snap_ln", "snap_sn",
       "vt_all", "vc_all", "vt_same", "vc_same", "wind_f", "cold_f", "cmp_l", "dypc_l"]


def player_features(df, stat, pos):
    d = df[df.position.isin(pos) & df[stat].notna()][["player_id", "player_display_name", "position", "grp", "team",
                                                      "opponent_team", "season", "week", "order", "game_id", stat] + CTX].copy()
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


def design(mu, sd, f, n, home, line, stat, imp, ex=None):
    """imp = the team's implied points from the closing spread and total (game script).
    ex = dict of extras() inputs (usage, teammates out, weather, formula); None = all neutral."""
    mu_adj = mu * f
    z = (mu_adj - line) / sd
    li = np.log(np.clip(imp, 8, 45) / 22.5)
    base = np.column_stack([z, z * np.abs(z), np.log(f), np.log1p(n), z * np.log1p(n), home,
                            np.log1p(line) - np.log1p(np.maximum(mu_adj, 0)), li, li * z])
    k = len(z)
    if ex is None:
        ex = {}
    get = lambda key, d=np.nan: np.broadcast_to(np.asarray(ex.get(key, d), float), (k,))  # noqa: E731
    E = extras(stat, z, sd, line, get("sh_s"), get("sh_l"), get("snap_s"), get("snap_l"), get("v_all", 0), get("v_same", 0),
               get("wind", 0), get("cold", 0), get("cmp"), get("dypc"), get("tsh"))
    return np.column_stack([base, E])


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
    sh = SHARE.get(stat, "tsh")
    vk = "vc" if sh == "csh" else "vt"
    ex = {"sh_s": dd[sh + "_s"], "sh_l": dd[sh + "_l"], "snap_s": dd.snap_s, "snap_l": dd.snap_l,
          "v_all": dd[vk + "_all"], "v_same": dd[vk + "_same"], "wind": dd.wind_f, "cold": dd.cold_f,
          "cmp": dd.cmp_l, "dypc": dd.dypc_l, "tsh": dd.tsh_l}
    X = design(dd.mu.to_numpy(), sd.loc[r.idx].to_numpy(), dd.f.to_numpy(), dd.n.to_numpy(),
               dd.home.to_numpy(), r.line.to_numpy(), stat, dd.imp.to_numpy(), {k: v.to_numpy() for k, v in ex.items()})
    return X, r.hit.to_numpy(), dd.season.to_numpy(), r


def main():
    df, team = load()
    # home/away from nflverse game_id: <season>_<wk>_<AWAY>_<HOME>
    df["home"] = (df.game_id.str.split("_").str[3] == df.team).astype(float)
    global IMP
    IMP = implied_points()
    out = {"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "stats": {}, "players": {}}
    report = [f"Player prop model, out-of-sample test seasons {TEST[0]}-{TEST[1]} (fit on {TRAIN[0]}-{TRAIN[1]})",
              "Brier score: lower is better. 'before' = same model without usage, snaps, teammates-out, weather and formula inputs.\n"]
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
        m = LogisticRegression(C=1.0, max_iter=1000).fit(X[tr], y[tr])
        p = m.predict_proba(X[te])[:, 1]
        base = y[tr].mean()
        brier = np.mean((p - y[te]) ** 2)
        brier0 = np.mean((base - y[te]) ** 2)
        mb = LogisticRegression(C=1.0, max_iter=1000).fit(X[tr][:, :-N_EXTRA], y[tr])
        brier_b = np.mean((mb.predict_proba(X[te][:, :-N_EXTRA])[:, 1] - y[te]) ** 2)
        # naive player-only baseline: same features without the logistic calibration
        zte = X[te][:, 0]
        from scipy.stats import norm
        brier_n = np.mean((norm.cdf(zte) - y[te]) ** 2)
        report.append(f"{stat:16} n={te.sum():7d}  Brier model {brier:.4f} (before {brier_b:.4f}, {(brier_b - brier) / brier_b * 100:+.1f}% better)"
                      f" | plain normal curve {brier_n:.4f} | coin-flip base {brier0:.4f}")
        bins = np.clip((p * 10).astype(int), 0, 9)
        cal = []
        for b in range(10):
            s = bins == b
            if s.sum() >= 200:
                cal.append(f"   predicted {b*10:2d}-{b*10+9:2d}%: actual {y[te][s].mean()*100:5.1f}%  (n={s.sum()})")
        report.extend(cal)
        # refit on everything through the latest finished season for live use
        allm = season <= latest_season
        m = LogisticRegression(C=1.0, max_iter=1000).fit(X[allm], y[allm])
        out["stats"][stat] = {"coef": [round(float(c), 6) for c in m.coef_[0]], "intercept": round(float(m.intercept_[0]), 6),
                              "sd_floor": SD_FLOOR[stat]}
        # current features per active player: latest game this season, "now" values
        cur = player_features(df, stat, pos)
        cur = cur[cur.season == latest_season].groupby("player_id").tail(1)
        sdn = np.sqrt(np.maximum(cur.m2_now - cur.mu_now ** 2, 0) + SD_FLOOR[stat] ** 2)
        for (_, row), s_ in zip(cur.iterrows(), sdn):
            pl = out["players"].setdefault(row.player_display_name, {"id": row.player_id, "team": row.team, "grp": row.grp})
            pl[stat] = [round(float(row.mu_now), 2), round(float(s_), 2), int(row.n) + 1]
            r4 = lambda v: None if pd.isna(v) else round(float(v), 4)  # noqa: E731
            pl["u"] = {k: r4(getattr(row, k + "n")) for k in ("tsh_l", "tsh_s", "csh_l", "csh_s", "snap_l", "snap_s")}
            pl["last_gid"] = row.game_id
        # current defense factors for this stat
        dn = dfac.sort_values("order").groupby(["opponent_team", "grp"]).tail(1)
        out.setdefault("defense", {})[stat] = {f"{r.opponent_team}|{r.grp}": round(float(r.f_now), 3) for r in dn.itertuples()}
        print(report[-1 - len(cal)])
    tr = json.loads((HERE / "site" / "data" / "trends.json").read_text())
    out["ctx"] = live_context(df, team, tr, out["players"])
    out["wind_mean"] = round(WIND_MEAN, 2)
    out["implied"] = {}
    for g in tr["week_games"]:
        if g.get("spread_line") is not None and g.get("total_line") is not None:
            out["implied"][g["game_id"]] = {g["home"]: g["total_line"] / 2 + g["spread_line"] / 2, g["away"]: g["total_line"] / 2 - g["spread_line"] / 2}
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    REPORT.parent.mkdir(exist_ok=True)
    REPORT.write_text("\n".join(report) + "\n")
    print(f"-> {OUT} ({len(out['players'])} players)")


def live_context(df, team, tr, players):
    """This week's inputs that aren't per-player: teammates out (injury report), weather, team passing.
    {gid: {"wind", "cold", "label", teams: {TEAM: {vt, vc, vt_g: {grp}, vc_g: {grp}, cmp, out: [names]}}}, "dypc": {TEAM: ypc}}"""
    ctx = {"games": {}, "dypc": {}}
    last = team.sort_values("order").groupby("team").tail(1)
    cmp_now = dict(zip(last.team, last.cmp_ln))
    dlast = team.sort_values("order").groupby("opponent_team").tail(1)
    ctx["dypc"] = {t: round(float(v), 3) for t, v in zip(dlast.opponent_team, dlast.dypc_ln) if pd.notna(v)}
    season = int(df.season.max())
    out_ids = set()
    injf = HERE / "data" / "injuries" / f"injuries_{season}.csv"
    if injf.exists():
        inj = pd.read_csv(injf, low_memory=False)
        wk = tr["next_week"]
        out_ids = set(inj[(inj.week == wk) & inj.report_status.isin(["Out", "Doubtful"])].gsis_id.dropna())
    wx = {}
    wf = HERE / "site" / "data" / "weather.json"
    if wf.exists():
        wx = json.loads(wf.read_text()).get("games", {})
    cur = df[df.season == season]
    last_gid = cur.sort_values("order").groupby("team").game_id.last().to_dict()
    for g in tr["week_games"]:
        w = wx.get(g["game_id"], {})
        indoor = w.get("roof") in ("dome", "closed")
        wind = 0 if indoor else (w.get("wind") if w.get("wind") is not None else WIND_MEAN)
        cold = 0 if indoor or w.get("temp") is None else max(0, 40 - w["temp"])
        e = {"wind": round(wind / 10, 3), "cold": round(cold / 10, 3), "label": w.get("label", ""), "teams": {}}
        for t in (g["away"], g["home"]):
            lg = last_gid.get(t)
            prev = cur[(cur.game_id == lg) & (cur.team == t)]
            gone = prev[prev.player_id.isin(out_ids)]
            te = {"vt": round(float(gone.tsh_ln.sum()), 4), "vc": round(float(gone.csh_ln.sum()), 4),
                  "vt_g": {k: round(float(v), 4) for k, v in gone.groupby("grp").tsh_ln.sum().items()},
                  "vc_g": {k: round(float(v), 4) for k, v in gone.groupby("grp").csh_ln.sum().items()},
                  "cmp": None if pd.isna(cmp_now.get(t, np.nan)) else round(float(cmp_now[t]), 3),
                  "out": [f"{r.player_display_name} ({r.position})" for r in gone.itertuples() if max(r.tsh_ln, r.csh_ln) >= 0.05]}
            e["teams"][t] = te
        ctx["games"][g["game_id"]] = e
    return ctx


def live_ex(model, player, stat, gid, team, opp):
    """extras() inputs for one player this week (same as the website)."""
    pl = model["players"][player]
    u = pl.get("u", {})
    c = model.get("ctx", {})
    gc = c.get("games", {}).get(gid, {})
    tc = gc.get("teams", {}).get(team, {})
    sh = SHARE.get(stat, "tsh")
    vk = "vc" if sh == "csh" else "vt"
    nn = lambda v: np.nan if v is None else v  # noqa: E731
    return {"sh_s": nn(u.get(sh + "_s")), "sh_l": nn(u.get(sh + "_l")), "snap_s": nn(u.get("snap_s")), "snap_l": nn(u.get("snap_l")),
            "v_all": tc.get(vk, 0), "v_same": tc.get(vk + "_g", {}).get(pl["grp"], 0),
            "wind": gc.get("wind", model.get("wind_mean", WIND_MEAN) / 10), "cold": gc.get("cold", 0),
            "cmp": nn(tc.get("cmp")), "dypc": nn(c.get("dypc", {}).get(opp)), "tsh": nn(u.get("tsh_l"))}


def prob(model, player, stat, line, opp, home, imp=22.5, gid=None):
    """Python twin of the website's pricing, for props.py and grading."""
    st = model["stats"].get(stat)
    pl = model["players"].get(player)
    if not st or not pl or stat not in pl:
        return None
    mu, sd, n = pl[stat]
    f = model["defense"][stat].get(f"{opp}|{pl['grp']}", 1.0)
    ex = live_ex(model, player, stat, gid, pl["team"], opp) if gid else None
    x = design(np.array([mu]), np.array([sd]), np.array([f]), np.array([n]), np.array([float(home)]), np.array([line]), stat, np.array([imp]), ex)[0]
    z = st["intercept"] + float(np.dot(st["coef"], x))
    return 1 / (1 + np.exp(-z))


if __name__ == "__main__":
    main()
