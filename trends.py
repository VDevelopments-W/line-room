#!/usr/bin/env python3
"""
Trend engine: backtests classic NFL betting trends against every game since
1999 (closing lines from nflverse), and lists this week's games that match.

Conventions (nflverse):
  spread_line = expected home margin (positive = home favored)
  result      = home score - away score
  Home side covers when result > spread_line. Over hits when total > total_line.

Every trend is a rule that picks one side of a game. Records assume -110
(teaser legs: 2-team 6-point teaser at -120, breakeven 73.9% per leg).

Usage
  python3 trends.py            # print the table, write site/data/trends.json
"""
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ingest import TEAM_TZ

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl.db"
OUT = HERE / "site" / "data" / "trends.json"

HIST_FIRST, HIST_LAST = 1999, 2025
SPLIT_AT = 2013            # consistency check: 1999-2012 vs 2013-2025
RECENT = 5                 # last N completed seasons
BE_110 = 110 / 210         # 52.38% breakeven at -110
BE_TEASE = math.sqrt(1 / (1 + 100 / 120))  # 73.85% per leg, 2-team at -120


# ----------------------------------------------------------------- data prep

def load():
    con = sqlite3.connect(DB_PATH)
    g = pd.read_sql("SELECT * FROM games WHERE spread_line IS NOT NULL "
                    "AND total_line IS NOT NULL", con)
    con.close()
    g["gametime"] = g["gametime"].fillna("13:00")
    g["hour"] = g["gametime"].str.slice(0, 2).astype(int)
    g["primetime"] = (g["hour"] >= 19) | g["weekday"].isin(["Thursday", "Monday"])
    g["done"] = g["result"].notna()
    g["tz_home"] = g["home_team"].map(TEAM_TZ).fillna("ET")
    g["tz_away"] = g["away_team"].map(TEAM_TZ).fillna("ET")
    g["outdoor"] = g["roof"].isin(["outdoors", "open"])
    g = g.sort_values(["season", "gameday", "gametime"]).reset_index(drop=True)

    # Previous game in the same season for each team (regular + playoffs)
    rows = []
    for side, opp in (("home", "away"), ("away", "home")):
        t = g[["game_id", "season", "gameday", f"{side}_team", "result", "spread_line"]].copy()
        t.columns = ["game_id", "season", "gameday", "team", "result", "spread_line"]
        sign = 1 if side == "home" else -1
        t["margin"] = sign * t["result"]
        t["ats"] = sign * (t["result"] - t["spread_line"])     # >0 covered
        t["was_dog"] = (sign * t["spread_line"]) < 0
        t["side"] = side
        rows.append(t)
    tg = pd.concat(rows).sort_values(["team", "season", "gameday"])
    for col in ("margin", "ats", "was_dog"):
        tg[f"prev_{col}"] = tg.groupby(["team", "season"])[col].shift(1)
    tg["game_no"] = tg.groupby(["team", "season"]).cumcount() + 1
    for side in ("home", "away"):
        sub = tg[tg.side == side].set_index("game_id")
        for col in ("prev_margin", "prev_ats", "prev_was_dog", "game_no"):
            g[f"{side}_{col}"] = g["game_id"].map(sub[col])

    # Division rematch: second meeting of the same pair in a season
    pair = g.apply(lambda r: "-".join(sorted([r.home_team, r.away_team])), axis=1)
    g["meeting_no"] = g.assign(pair=pair).groupby(["season", "pair"]).cumcount() + 1
    return g


# ----------------------------------------------------------------- bet helpers

def ats(df, side):
    """Spread bets on 'home' or 'away' (side may be a Series)."""
    side = side if isinstance(side, pd.Series) else pd.Series(side, index=df.index)
    diff = np.where(side == "home", df["result"] - df["spread_line"],
                    df["spread_line"] - df["result"])
    team = np.where(side == "home", df["home_team"], df["away_team"])
    line = np.where(side == "home", -df["spread_line"], df["spread_line"])
    return pd.DataFrame({"game_id": df["game_id"], "diff": diff, "pick": [
        f"{t} {fmt_line(l)}" for t, l in zip(team, line)], "kind": "spread", "side": side.values})


def ou(df, side):
    diff = (df["total"] - df["total_line"]) * (1 if side == "over" else -1)
    word = "Over" if side == "over" else "Under"
    return pd.DataFrame({"game_id": df["game_id"], "diff": diff,
                         "pick": [f"{word} {l:g}" for l in df["total_line"]], "kind": "total", "side": side})


def tease(df, side, pts=6):
    side = side if isinstance(side, pd.Series) else pd.Series(side, index=df.index)
    base = np.where(side == "home", df["result"] - df["spread_line"],
                    df["spread_line"] - df["result"])
    team = np.where(side == "home", df["home_team"], df["away_team"])
    line = np.where(side == "home", -df["spread_line"], df["spread_line"]) + pts
    return pd.DataFrame({"game_id": df["game_id"], "diff": base + pts, "pick": [
        f"{t} {fmt_line(l)} (teased)" for t, l in zip(team, line)], "kind": "teaser", "side": side.values})


def fmt_line(l):
    if l == 0:
        return "PK"
    return f"+{l:g}" if l > 0 else f"{l:g}"


def fav_side(df):
    return pd.Series(np.where(df["spread_line"] > 0, "home", "away"), index=df.index)


def dog_side(df):
    return pd.Series(np.where(df["spread_line"] > 0, "away", "home"), index=df.index)


# ----------------------------------------------------------------- trends

def T(id, name, category, desc, fn):
    return {"id": id, "name": name, "category": category, "desc": desc, "fn": fn}


def reg(df):
    return df[df.game_type == "REG"]


TRENDS = [
    # Underdogs / favorites
    T("home_dog", "Home underdogs", "Spread", "Bet every home team getting points.",
      lambda d: (lambda x: ats(x, "home"))(reg(d)[reg(d).spread_line < 0])),
    T("home_dog_7", "Big home underdogs (+7 or more)", "Spread",
      "Home teams getting 7+ points. The public loves big road favorites.",
      lambda d: (lambda x: ats(x, "home"))(reg(d)[reg(d).spread_line <= -7])),
    T("road_fav_7", "Big road favorites (-7 or more)", "Spread",
      "Road teams laying 7+ points.",
      lambda d: (lambda x: ats(x, "away"))(reg(d)[reg(d).spread_line <= -7])),
    T("home_fav_10", "Big home favorites (-10 or more)", "Spread",
      "Home teams laying double digits.",
      lambda d: (lambda x: ats(x, "home"))(reg(d)[reg(d).spread_line >= 10])),
    T("dog_3", "Underdogs getting exactly +3", "Spread",
      "3 is the most common NFL margin, so +3 has extra value.",
      lambda d: (lambda x: ats(x, dog_side(x)))(reg(d)[reg(d).spread_line.abs() == 3])),
    T("div_dog", "Division underdogs", "Spread",
      "Underdogs in division games. Familiar opponents keep games close.",
      lambda d: (lambda x: ats(x, dog_side(x)))(reg(d)[(reg(d).div_game == 1) & (reg(d).spread_line != 0)])),
    T("div_dog_7", "Division underdogs +7 or more", "Spread",
      "Big underdogs in division games.",
      lambda d: (lambda x: ats(x, dog_side(x)))(reg(d)[(reg(d).div_game == 1) & (reg(d).spread_line.abs() >= 7)])),
    T("late_season_home_dog", "Home dogs, Week 13+", "Spread",
      "Home underdogs late in the season.",
      lambda d: (lambda x: ats(x, "home"))(reg(d)[(reg(d).spread_line < 0) & (reg(d).week >= 13)])),
    T("tnf_dog", "Thursday night underdogs", "Spread",
      "Underdogs on Thursday night. Short week tends to compress games.",
      lambda d: (lambda x: ats(x, dog_side(x)))(reg(d)[(reg(d).weekday == "Thursday") & (reg(d).spread_line != 0)])),
    T("mnf_home_dog", "Monday night home dogs", "Spread", "Home underdogs on Monday night.",
      lambda d: (lambda x: ats(x, "home"))(reg(d)[(reg(d).weekday == "Monday") & (reg(d).spread_line < 0)])),

    # Rest / schedule
    T("off_bye", "Teams off a bye", "Schedule",
      "Team had 13+ days rest, opponent did not.",
      lambda d: (lambda x: ats(x, pd.Series(np.where(x.home_rest >= 13, "home", "away"), index=x.index)))(
          reg(d)[(reg(d).home_rest >= 13) ^ (reg(d).away_rest >= 13)])),
    T("off_bye_fav", "Favorites off a bye", "Schedule",
      "Favorite had 13+ days rest, opponent did not.",
      lambda d: (lambda x: ats(x, fav_side(x)))(
          reg(d)[(((reg(d).spread_line > 0) & (reg(d).home_rest >= 13) & (reg(d).away_rest < 13)) |
                  ((reg(d).spread_line < 0) & (reg(d).away_rest >= 13) & (reg(d).home_rest < 13)))])),
    T("rest_edge", "Big rest advantage (3+ days)", "Schedule",
      "Bet the team with 3+ more days of rest.",
      lambda d: (lambda x: ats(x, pd.Series(np.where(x.home_rest > x.away_rest, "home", "away"), index=x.index)))(
          reg(d)[(reg(d).home_rest - reg(d).away_rest).abs() >= 3])),
    T("west_early", "West Coast teams at 1 PM ET", "Schedule",
      "Pacific-time road teams playing a 1 PM ET game in the East (body clock at 10 AM).",
      lambda d: (lambda x: ats(x, "away"))(reg(d)[(reg(d).tz_away == "PT") & (reg(d).tz_home == "ET") & (reg(d).hour == 13)])),

    # Situational
    T("off_blowout_loss", "Off a 20+ point loss", "Situational",
      "Bet a team the week after it lost by 20+. Markets overreact to blowouts.",
      lambda d: (lambda x: ats(x, pd.Series(np.where(x.home_prev_margin <= -20, "home", "away"), index=x.index)))(
          reg(d)[(reg(d).home_prev_margin <= -20) ^ (reg(d).away_prev_margin <= -20)])),
    T("off_blowout_win_fade", "Fade a team off a 20+ point win", "Situational",
      "Bet against a team the week after it won by 20+.",
      lambda d: (lambda x: ats(x, pd.Series(np.where(x.home_prev_margin >= 20, "away", "home"), index=x.index)))(
          reg(d)[(reg(d).home_prev_margin >= 20) ^ (reg(d).away_prev_margin >= 20)])),
    T("off_upset_fade", "Fade a team off an outright upset as a 7+ dog", "Situational",
      "Bet against a team the week after it won outright as a 7+ point underdog.",
      lambda d: (lambda x: ats(x, pd.Series(np.where(
          (x.home_prev_margin > 0) & (x.home_prev_ats >= 7) & (x.home_prev_was_dog == True), "away", "home"), index=x.index)))(
          reg(d)[((reg(d).home_prev_margin > 0) & (reg(d).home_prev_was_dog == True) & (reg(d).home_prev_margin - reg(d).home_prev_ats <= -7)) ^
                 ((reg(d).away_prev_margin > 0) & (reg(d).away_prev_was_dog == True) & (reg(d).away_prev_margin - reg(d).away_prev_ats <= -7))])),
    T("week1_dog", "Week 1 underdogs", "Situational", "Underdogs in Week 1.",
      lambda d: (lambda x: ats(x, dog_side(x)))(reg(d)[(reg(d).week == 1) & (reg(d).spread_line != 0)])),

    # Totals
    T("prime_under", "Primetime unders", "Totals",
      "Unders in Thursday, Sunday and Monday night games. The public bets overs in big games.",
      lambda d: ou(reg(d)[reg(d).primetime], "under")),
    T("tnf_under", "Thursday night unders", "Totals", "Unders on Thursday night.",
      lambda d: ou(reg(d)[reg(d).weekday == "Thursday"], "under")),
    T("mnf_under", "Monday night unders", "Totals", "Unders on Monday night.",
      lambda d: ou(reg(d)[reg(d).weekday == "Monday"], "under")),
    T("snf_under", "Sunday night unders", "Totals", "Unders in Sunday night games.",
      lambda d: ou(reg(d)[(reg(d).weekday == "Sunday") & (reg(d).hour >= 19)], "under")),
    T("div_under", "Division game unders", "Totals", "Unders when division rivals meet.",
      lambda d: ou(reg(d)[reg(d).div_game == 1], "under")),
    T("div_rematch_under", "Division rematch unders", "Totals",
      "Unders in the second meeting of division rivals.",
      lambda d: ou(reg(d)[(reg(d).div_game == 1) & (reg(d).meeting_no == 2)], "under")),
    T("wind_under", "Wind 15+ mph unders", "Totals",
      "Unders in outdoor games with 15+ mph wind (needs a game-day forecast).",
      lambda d: ou(reg(d)[reg(d).outdoor & (reg(d).wind >= 15)], "under")),
    T("cold_under", "Freezing games (32F or colder) unders", "Totals",
      "Unders in outdoor games at or below freezing.",
      lambda d: ou(reg(d)[reg(d).outdoor & (reg(d).temp <= 32)], "under")),
    T("high_total_under", "High totals (50+) unders", "Totals",
      "Unders when the total is 50 or higher.",
      lambda d: ou(reg(d)[reg(d).total_line >= 50], "under")),
    T("low_total_over", "Low totals (38 or less) overs", "Totals",
      "Overs when the total is 38 or lower.",
      lambda d: ou(reg(d)[reg(d).total_line <= 38], "over")),
    T("dome_over", "Dome game overs", "Totals", "Overs in dome or closed-roof games.",
      lambda d: ou(reg(d)[reg(d).roof.isin(["dome", "closed"])], "over")),
    T("late_season_under", "Unders, Week 13+", "Totals", "Unders late in the season.",
      lambda d: ou(reg(d)[reg(d).week >= 13], "under")),
    T("playoff_under", "Playoff unders", "Totals", "Unders in the playoffs.",
      lambda d: ou(d[d.game_type != "REG"], "under")),

    # Teasers
    T("wong_fav", "Wong teaser: favorites -7.5 to -8.5", "Teaser",
      "Tease favorites of -7.5 to -8.5 down 6 points through 7 and 3 (to -1.5 to -2.5). Each leg must win 73.9% to profit.",
      lambda d: (lambda x: tease(x, fav_side(x)))(reg(d)[reg(d).spread_line.abs().between(7.5, 8.5)])),
    T("wong_dog", "Wong teaser: underdogs +1.5 to +2.5", "Teaser",
      "Tease underdogs of +1.5 to +2.5 up 6 points through 3 and 7 (to +7.5 to +8.5).",
      lambda d: (lambda x: tease(x, dog_side(x)))(reg(d)[reg(d).spread_line.abs().between(1.5, 2.5)])),
    T("wong_dog_lowtotal", "Wong teaser dogs, total under 49", "Teaser",
      "Wong underdog legs only when the total is under 49 (fewer points makes 7 and 3 matter more).",
      lambda d: (lambda x: tease(x, dog_side(x)))(reg(d)[reg(d).spread_line.abs().between(1.5, 2.5) & (reg(d).total_line < 49)])),
]


# ----------------------------------------------------------------- stats

def record(bets):
    w = int((bets["diff"] > 0).sum())
    l = int((bets["diff"] < 0).sum())
    p = int((bets["diff"] == 0).sum())
    return w, l, p


def summarize(bets, games, kind):
    be = BE_TEASE if kind == "teaser" else BE_110
    win_pay = (100 / 120) if kind == "teaser" else (100 / 110)
    b = bets.merge(games[["game_id", "season", "week", "done"]], on="game_id")
    hist = b[b.done & b.season.between(HIST_FIRST, HIST_LAST)]

    def stat(df):
        w, l, p = record(df)
        n = w + l
        pct = w / n if n else None
        if kind == "teaser":
            # Units for 2-team teasers: pair legs randomly is noisy, so show the
            # expected ROI of 2-leg tickets at this leg win rate.
            units = None
            roi = (pct ** 2 * (1 + win_pay) - 1) if pct is not None else None
        else:
            units = round(w * win_pay - l, 1)
            roi = units / n if n else None
        z = (w - n * be) / math.sqrt(n * be * (1 - be)) if n else None
        return {"w": w, "l": l, "p": p, "pct": pct, "units": units, "roi": roi, "z": z}

    by_season = []
    for s, df in hist.groupby("season"):
        st = stat(df)
        by_season.append({"season": int(s), "w": st["w"], "l": st["l"], "p": st["p"], "pct": st["pct"]})
    early = stat(hist[hist.season < SPLIT_AT])
    late = stat(hist[hist.season >= SPLIT_AT])
    recent = stat(hist[hist.season > HIST_LAST - RECENT])
    cur = b[b.done & (b.season == HIST_LAST + 1)]
    allt = stat(hist)
    prof = sum(1 for s in by_season if s["pct"] is not None and s["pct"] > be)
    return {
        "all": allt, "early": early, "late": late, "recent": recent, "current": stat(cur),
        "by_season": by_season, "profitable_seasons": prof, "seasons": len(by_season),
        "breakeven": be, "verdict": verdict(allt, early, late, recent, be),
    }


def verdict(allt, early, late, recent, be):
    """Strong: beats breakeven by 2+ standard errors and held up in both
    halves and the last 5 seasons. Lean: profitable overall, in 2013-2025 and
    in the last 5 seasons. Fade it: betting the OTHER side clears the same
    bar as Strong (spread/total only)."""
    if allt["pct"] is None:
        return "No data"
    parts = (early, late, recent)
    if allt["z"] >= 2 and all(p["pct"] is not None and p["pct"] > be for p in parts):
        return "Strong"
    if be < 0.6:  # fading only makes sense for -110 bets, not teaser legs
        n = allt["w"] + allt["l"]
        z_fade = (allt["l"] - n * be) / math.sqrt(n * be * (1 - be))
        if z_fade >= 2 and all(p["pct"] is not None and (1 - p["pct"]) > be for p in parts):
            return "Fade it"
    if allt["pct"] > be and all(p["pct"] is not None and p["pct"] > be for p in (late, recent)):
        return "Lean"
    return "No edge"


def upcoming(bets, games):
    b = bets.merge(games, on="game_id")
    b = b[~b.done]
    out = []
    for _, r in b.iterrows():
        out.append({"game_id": r.game_id, "season": int(r.season), "week": int(r.week),
                    "gameday": r.gameday, "gametime": r.gametime, "away": r.away_team,
                    "home": r.home_team, "pick": r["pick"], "kind": r["kind"], "side": r["side"]})
    return out


def run():
    games = load()
    results = []
    for t in TRENDS:
        bets = t["fn"](games)
        kind = bets["kind"].iloc[0] if len(bets) else "spread"
        s = summarize(bets, games, kind)
        results.append({k: v for k, v in t.items() if k != "fn"} | {
            "kind": kind, **s, "upcoming": upcoming(bets, games)})

    # Games for "this week": the next week with lines and no scores
    pending = games[~games.done & (games.season == HIST_LAST + 1)]
    next_week = int(pending.week.min()) if len(pending) else None
    week_games = []
    if next_week is not None:
        for _, r in pending[pending.week == next_week].iterrows():
            week_games.append({
                "game_id": r.game_id, "week": int(r.week), "gameday": r.gameday,
                "gametime": r.gametime, "weekday": r.weekday, "away": r.away_team,
                "home": r.home_team, "spread_line": r.spread_line, "total_line": r.total_line,
                "away_ml": None if pd.isna(r.away_moneyline) else int(r.away_moneyline),
                "home_ml": None if pd.isna(r.home_moneyline) else int(r.home_moneyline),
                "roof": r.roof, "div_game": int(r.div_game),
                "away_rest": int(r.away_rest), "home_rest": int(r.home_rest)})

    con = sqlite3.connect(DB_PATH)
    fades = pd.read_sql("SELECT f.*, g.away_team, g.home_team, g.away_score, g.home_score "
                        "FROM fade_plays f JOIN games g USING(game_id) ORDER BY week", con)
    n_games = con.execute("SELECT COUNT(*) FROM games WHERE result IS NOT NULL").fetchone()[0]
    con.close()

    cur = games[games.season == HIST_LAST + 1]
    season_games = [{
        "game_id": r.game_id, "week": int(r.week), "gameday": r.gameday, "gametime": r.gametime,
        "away": r.away_team, "home": r.home_team,
        "away_score": None if pd.isna(r.away_score) else int(r.away_score),
        "home_score": None if pd.isna(r.home_score) else int(r.home_score),
        "spread_line": r.spread_line, "total_line": r.total_line} for _, r in cur.iterrows()]

    data = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "season_games": season_games,
        "history": {"first": HIST_FIRST, "last": HIST_LAST, "games": n_games, "split_at": SPLIT_AT,
                    "recent": RECENT},
        "season": HIST_LAST + 1, "next_week": next_week, "week_games": week_games,
        "trends": results,
        "fade_plays": json.loads(fades.to_json(orient="records")),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(_clean(data), default=_json_default, allow_nan=False))
    return data


def _clean(o):
    """Replace NaN/inf with None so the file is valid JSON."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    if isinstance(o, (float, np.floating)) and not math.isfinite(float(o)):
        return None
    return o


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if np.isnan(o) else float(o)
    if isinstance(o, (np.bool_,)):
        return bool(o)
    raise TypeError(type(o))


def pct(x):
    return "  -  " if x is None else f"{x * 100:5.1f}"


if __name__ == "__main__":
    data = run()
    print(f"{'Trend':48} {'Record':>14} {'Win%':>6} {'Units':>7} {'z':>5} "
          f"{'99-12':>6} {'13-25':>6} {'L5':>6} {'2026':>9}  Verdict   Wk{data['next_week']}")
    for t in sorted(data["trends"], key=lambda t: -(t["all"]["z"] or 0)):
        a = t["all"]
        rec = f"{a['w']}-{a['l']}-{a['p']}"
        c = t["current"]
        units = "" if a["units"] is None else f"{a['units']:+.1f}"
        wk = sum(1 for u in t["upcoming"] if u["week"] == data["next_week"])
        print(f"{t['name'][:48]:48} {rec:>14} {pct(a['pct']):>6} {units:>7} {a['z']:5.1f} "
              f"{pct(t['early']['pct']):>6} {pct(t['late']['pct']):>6} {pct(t['recent']['pct']):>6} "
              f"{c['w']}-{c['l']}-{c['p']:<4}  {t['verdict']:9} {wk}")
    print(f"\nWrote {OUT}")
