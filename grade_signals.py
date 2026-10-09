#!/usr/bin/env python3
"""
Grade every signal Line Room produces, so we learn which ones are real.

Signals (2026 season, from the weeks this pipeline has been running):
  fade_dk      80%+ of DraftKings bets on a spread/total side at the last pre-kickoff snapshot -> bet the other side
  fade_sao     same at Scores and Odds only (DraftKings under 80%)
  fade_both    80%+ at both
  fade_70      70-79% at DraftKings or Scores and Odds (watch tier, not bet)
  model_side   power-rating model 3+ points off the spread or total (archived before kickoff)
  prop_model   player prop model 5+ points above the closing Kalshi price (liquid markets)
  prop_streak  streak board's pooled history 3+ points above the closing Kalshi price, exact line

Each graded item also gets closing line value (CLV): the number we would have bet versus the
closing number in nflverse (spreads/totals, in points; positive = we beat the close).
Spreads and totals are scored at -110; props by Kalshi price (pay the ask, collect $1 if it hits).

Writes site/data/signals.json.
"""
import glob
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "data" / "signals.json"
LABELS = {
    "fade_both": ("Fade: both sources 80%+", "DraftKings and Scores and Odds both had 80%+ of bets on the side we fade."),
    "fade_dk": ("Fade: DraftKings 80%+", "80%+ of DraftKings bets, Scores and Odds under 80%."),
    "fade_sao": ("Fade: Scores and Odds 80%+", "80%+ at Scores and Odds, DraftKings under 80% (or not recorded, Weeks 1-4)."),
    "fade_70": ("Fade watch: 70-79%", "70-79% at either source. Tracked, not bet."),
    "model_side": ("Model: 3+ points off the line", "Power ratings disagree with the spread or total by 3+ points."),
    "prop_model": ("Props: model beats Kalshi by 5+", "Player prop model's chance is 5 to 15 points above Kalshi's closing price (bigger gaps usually mean news)."),
    "prop_streak": ("Props: streak history beats Kalshi by 3+", "The streak board's pooled history beats Kalshi's closing price by 3+ points."),
}


def games():
    con = sqlite3.connect(HERE / "nfl.db")
    df = pd.read_sql("SELECT game_id, season, week, away_team, home_team, away_score, home_score, spread_line, total_line, gameday FROM games", con)
    con.close()
    return {r.game_id: r for r in df.itertuples()}


def grade_side(g, market, side, line):
    if pd.isna(g.home_score):
        return None
    if market == "total":
        d = (g.home_score + g.away_score) - line
        d = d if side == "over" else -d
    else:
        margin = g.home_score - g.away_score if side == "home" else g.away_score - g.home_score
        d = margin + line
    return "W" if d > 0 else "L" if d < 0 else "P"


def close_line(g, market, side):
    if market == "total":
        return g.total_line
    return -g.spread_line if side == "home" else g.spread_line


def clv(market, side, line, close):
    if close is None or pd.isna(close) or line is None:
        return None
    if market == "total":
        return round((close - line) if side == "over" else (line - close), 1)
    return round(line - close, 1)  # spread: our line (e.g. +3.5) minus closing line for that side (+2.5) = +1


OPP = {"home": "away", "away": "home", "over": "under", "under": "over"}


def fades(G):
    items = []
    for f in sorted(glob.glob(str(HERE / "data" / "splits_history" / "*.json"))):
        h = json.loads(Path(f).read_text())
        g = G.get(h["game_id"])
        if g is None or not h["snaps"]:
            continue
        rows = h["snaps"][-1]["rows"]
        for market in ("spread", "total"):
            by = {(r["source"], r["side"]): r for r in rows if r["market"] == market}
            for pub in (("home", "away") if market == "spread" else ("over", "under")):
                dk, sao = by.get(("dk", pub)), by.get(("sao", pub))
                bd, bs = (dk or {}).get("bets") or 0, (sao or {}).get("bets") or 0
                if bd >= 80 and bs >= 80:
                    key = "fade_both"
                elif bd >= 80:
                    key = "fade_dk"
                elif bs >= 80:
                    key = "fade_sao"
                elif bd >= 70 or bs >= 70:
                    key = "fade_70"
                else:
                    continue
                side = OPP[pub]
                src = by.get(("dk", side)) or by.get(("sao", side)) or {}
                line = src.get("line")
                if line is None:  # Scores and Odds-only row without a line: use the public side's line flipped
                    pl = (dk or sao or {}).get("line")
                    line = pl if market == "total" else (-pl if pl is not None else None)
                if line is None:
                    continue
                res = grade_side(g, market, side, line)
                if res is None:
                    continue
                name = f"{'Over' if side == 'over' else 'Under'} {line}" if market == "total" else f"{getattr(g, side + '_team')} {line:+g}"
                items.append({"key": key, "week": int(g.week), "game": f"{g.away_team} @ {g.home_team}", "pick": name, "res": res,
                              "clv": clv(market, side, line, close_line(g, market, side)),
                              "note": f"public {pub} DK {bd}% / S&O {bs}%"})
    return items


def model_sides(G):
    items = []
    for f in sorted(glob.glob(str(HERE / "data" / "archive" / "*" / "model.json"))):
        for gid, m in json.loads(Path(f).read_text()).items():
            g = G.get(gid)
            if g is None or m.get("spread_line") is None:
                continue
            # spread: margin and spread_line are both "home expected margin"
            e = m["margin"] - m["spread_line"]
            if abs(e) >= 3:
                side = "home" if e > 0 else "away"
                line = -m["spread_line"] if side == "home" else m["spread_line"]
                res = grade_side(g, "spread", side, line)
                if res:
                    items.append({"key": "model_side", "week": int(g.week), "game": f"{g.away_team} @ {g.home_team}",
                                  "pick": f"{getattr(g, side + '_team')} {line:+g}", "res": res,
                                  "clv": clv("spread", side, line, close_line(g, "spread", side)), "note": f"model edge {e:+.1f}"})
            if m.get("total_line") is not None and m.get("total_proj") is not None:
                e = m["total_proj"] - m["total_line"]
                if abs(e) >= 3:
                    side = "over" if e > 0 else "under"
                    res = grade_side(g, "total", side, m["total_line"])
                    if res:
                        items.append({"key": "model_side", "week": int(g.week), "game": f"{g.away_team} @ {g.home_team}",
                                      "pick": f"{side.title()} {m['total_line']}", "res": res,
                                      "clv": clv("total", side, m["total_line"], g.total_line), "note": f"model edge {e:+.1f}"})
    return items


def player_stats(season):
    f = HERE / "data" / "players" / f"w{season}.csv.gz"
    if not f.exists():
        return {}
    d = pd.read_csv(f, low_memory=False)
    out = {}
    for r in d.itertuples():
        out[(r.game_id, r.player_display_name)] = r
    return out


def props(G):
    items = []
    stats_cache = {}
    for f in sorted(glob.glob(str(HERE / "data" / "archive" / "*" / "prop_prices.json"))):
        arch = Path(f).parent
        streaks = json.loads((arch / "streaks.json").read_text()) if (arch / "streaks.json").exists() else {}
        for gid, rows in json.loads(Path(f).read_text()).items():
            g = G.get(gid)
            if g is None or pd.isna(g.home_score):
                continue
            close = HERE / "data" / "kalshi_close" / f"{gid}.json"
            cl = {(r["player"], r["stat"], r["line"]): r for r in json.loads(close.read_text())["rows"]} if close.exists() else {}
            season = int(g.season)
            if season not in stats_cache:
                stats_cache[season] = player_stats(season)
            ps = stats_cache[season]
            hist = {(r["player"], r["stat"], r["line"]): r["hit_hist"] for r in streaks.get(gid, []) if r.get("hit_hist") is not None}
            for r in rows:
                c = cl.get((r["player"], r["stat"], r["line"]), r)
                ask, bid, vol = c.get("ask"), c.get("bid"), c.get("volume") or 0
                if ask is None or bid is None or ask <= 0 or ask >= 1 or ask - bid > 0.06 or vol < 50:
                    continue
                st = ps.get((gid, r["player"]))
                if st is None:
                    continue  # didn't play (Kalshi settles these at its pre-game price; skip)
                val = getattr(st, r["stat"], None)
                if val is None or pd.isna(val):
                    continue
                hit = val >= r["line"]
                base = {"week": int(g.week), "game": f"{g.away_team} @ {g.home_team}",
                        "pick": f"{r['player']} {r['line']}+ {r['stat'].replace('_', ' ')}", "res": "W" if hit else "L",
                        "price": ask, "actual": float(val)}
                if 0.05 <= r["p"] - ask <= 0.15:
                    items.append({**base, "key": "prop_model", "note": f"model {r['p']*100:.0f}% vs {ask*100:.0f}¢"})
                hh = hist.get((r["player"], r["stat"], r["line"]))
                if hh is not None and hh - ask >= 0.03:
                    items.append({**base, "key": "prop_streak", "note": f"history {hh*100:.0f}% vs {ask*100:.0f}¢"})
    return items


def summarize(items):
    groups = []
    for key, (label, desc) in LABELS.items():
        its = [i for i in items if i["key"] == key]
        w = sum(i["res"] == "W" for i in its)
        l = sum(i["res"] == "L" for i in its)
        p = sum(i["res"] == "P" for i in its)
        if key.startswith("prop"):
            staked = sum(i["price"] for i in its)
            profit = sum((1 - i["price"]) if i["res"] == "W" else -i["price"] for i in its)
            roi = profit / staked if staked else None
            units = None
        else:
            units = round(w * 100 / 110 - l, 2)
            roi = units / (w + l) if w + l else None
        cl = [i["clv"] for i in its if i.get("clv") is not None]
        weeks = sorted({i["week"] for i in its})
        groups.append({"key": key, "label": label, "desc": desc, "w": w, "l": l, "p": p,
                       "pct": round(w / (w + l), 4) if w + l else None, "units": units,
                       "roi": None if roi is None else round(roi, 4),
                       "clv": round(sum(cl) / len(cl), 2) if cl else None, "clv_n": len(cl),
                       "beat_close": round(sum(c > 0 for c in cl) / len(cl), 3) if cl else None,
                       "by_week": [{"week": wk, "w": sum(i["res"] == "W" for i in its if i["week"] == wk),
                                    "l": sum(i["res"] == "L" for i in its if i["week"] == wk)} for wk in weeks],
                       "items": sorted(its, key=lambda i: -i["week"])[:60]})
    return groups


def main():
    G = games()
    items = fades(G) + model_sides(G) + props(G)
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                               "groups": summarize(items)}, separators=(",", ":"), allow_nan=False))
    print(f"Signals: {len(items)} graded items -> {OUT}")
    for g in summarize(items):
        if g["w"] + g["l"]:
            print(f"  {g['label']:45} {g['w']}-{g['l']}-{g['p']}  CLV {g['clv']}")


if __name__ == "__main__":
    main()
