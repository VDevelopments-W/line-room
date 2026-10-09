#!/usr/bin/env python3
"""
Seed the fade_plays table with the 2026 public-fade plays collected so far,
then grade them from final scores in the games table.

Weeks 1-3: hand-collected from news articles quoting public splits
           (book and timing vary, see 'source').
Week 4+:   the live weekly test.

Each play: (week, away, home, market, public_side, line, bets, money, source,
            note, live_bet). public_side is 'home'/'away' for spreads and
'over'/'under' for totals. line is the public side's line (spreads) or the
total. The fade is the opposite side at the same number.
"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "nfl.db"
SEASON = 2026

PLAYS = [
    # Week 1: SportsBettingDime (book not named)
    (1, "WAS", "PHI", "total", "under", 44.5, 82.5, 82.1, "SBD", "", 0),
    (1, "TB", "CIN", "total", "under", 50.5, 88.0, 71.3, "SBD", "", 0),
    (1, "NO", "DET", "total", "under", 49.5, 81.9, 65.0, "SBD", "", 0),
    (1, "CHI", "CAR", "total", "under", 47.0, 86.8, 76.3, "SBD", "", 0),
    (1, "DAL", "NYG", "total", "under", 48.0, 86.0, 80.0, "SBD", "DK had 48.5", 0),
    (1, "DEN", "KC", "total", "under", 43.5, 80.0, 72.0, "SBD", "", 0),
    # Week 2: DraftKings, game day (OddsShopper)
    (2, "PHI", "TEN", "spread", "away", -7.0, 93, 94, "DK", "", 0),
    (2, "MIA", "SF", "spread", "home", -13.5, 87, 83, "DK", "", 0),
    (2, "CAR", "ATL", "spread", "away", -2.5, 84, 83, "DK", "", 0),
    (2, "CLE", "TB", "spread", "home", -8.5, 83, 95, "DK", "", 0),
    (2, "IND", "KC", "spread", "home", -6.5, 80, 80, "DK", "", 0),
    (2, "MIN", "CHI", "total", "over", 48.5, 80, 93, "DK", "", 0),
    (2, "JAX", "DEN", "total", "over", 45.5, 80, 83, "DK", "", 0),
    (2, "NO", "BAL", "total", "over", 46.5, 82, None, "DK", "", 0),
    # Week 3: DK early week (RotoWire/Covers) unless noted
    (3, "LAC", "BUF", "spread", "home", -7.0, 89, 97, "DK", "early week", 0),
    (3, "CIN", "PIT", "spread", "away", -3.5, 87, 98, "DK", "early week", 0),
    (3, "CAR", "CLE", "spread", "away", -2.5, 86, 97, "DK", "early week", 0),
    (3, "KC", "MIA", "spread", "away", -10.5, 84, 90, "DK", "early week", 0),
    (3, "SEA", "WAS", "spread", "away", -7.5, 89, 92, "SBD", "game day", 0),
    (3, "ARI", "SF", "spread", "home", -8.5, 81, 86, "DK", "early week", 0),
    (3, "BAL", "DAL", "total", "under", 52.5, 87, 81.8, "SBD", "", 0),
    # Week 4
    (4, "PIT", "CLE", "total", "over", 38.5, 83, 83, "SBD", "TNF", 0),
    (4, "IND", "WAS", "total", "over", 46.5, 92, 92, "SAO", "DK 58/55; live bet 0.5u", 1),
    (4, "LA", "PHI", "total", "over", 42.5, 82, 82, "DK", "live bet 1u", 1),
    (4, "DAL", "HOU", "spread", "away", 3.0, 81, 83, "SAO", "DK 71/37; live bet 0.5u", 1),
]


def grade(g, market, public_side, line):
    """Grade the FADE of public_side. Returns (fade_side, W/L/P) or None."""
    away, home = g
    if away is None or home is None:
        return None
    if market == "total":
        fade = "under" if public_side == "over" else "over"
        diff = (away + home) - line
        if fade == "under":
            diff = -diff
    else:
        fade = "home" if public_side == "away" else "away"
        # public side's line is `line`; fade side gets -line
        pub_margin = (home - away) if public_side == "home" else (away - home)
        diff = -(pub_margin + line)
    res = "W" if diff > 0 else ("L" if diff < 0 else "P")
    return fade, res


def main():
    con = sqlite3.connect(DB_PATH)
    con.execute("DELETE FROM fade_plays WHERE season=?", (SEASON,))
    for wk, away, home, market, pub, line, bets, money, src, note, live in PLAYS:
        row = con.execute("SELECT game_id, away_score, home_score FROM games "
                          "WHERE season=? AND week=? AND away_team=? AND home_team=?",
                          (SEASON, wk, away, home)).fetchone()
        if not row:
            print(f"!! game not found: wk{wk} {away}@{home}")
            continue
        gid, a, h = row
        gr = grade((a, h), market, pub, line)
        fade, res = gr if gr else ("", None)
        con.execute("INSERT INTO fade_plays VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (SEASON, wk, gid, market, pub, line, bets, money, src, fade, res, note, live))
    con.commit()
    for r in con.execute("SELECT week, COUNT(*), SUM(result='W'), SUM(result='L'), "
                         "SUM(result='P') FROM fade_plays GROUP BY week"):
        print(f"Week {r[0]}: {r[2]}-{r[3]}-{r[4]} ({r[1]} plays)")
    con.close()


if __name__ == "__main__":
    main()
