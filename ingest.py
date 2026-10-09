#!/usr/bin/env python3
"""
Build / refresh the NFL trends database (nfl.db, SQLite).

Sources
  - nflverse/nfldata games.csv: every game since 1999 with final score,
    closing spread, total, moneylines, rest days, weather, roof, division flag.
    Free and open source: https://github.com/nflverse/nfldata
  - Public betting splits are added separately by nfl_splits.py.

Usage
  python3 ingest.py                    # download latest games.csv and load it
  python3 ingest.py --csv games.csv    # load from a local copy
"""

import argparse
import io
import sqlite3
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl.db"
GAMES_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"

# Teams that relocated, mapped to their current code so history lines up
FRANCHISE = {"OAK": "LV", "SD": "LAC", "STL": "LA"}

# Home-stadium time zone of each franchise (current codes), used for the
# "West Coast team in a 1 PM ET game" trend.
TEAM_TZ = {
    "SEA": "PT", "SF": "PT", "LA": "PT", "LAC": "PT", "LV": "PT",
    "ARI": "MT", "DEN": "MT",
    "KC": "CT", "DAL": "CT", "HOU": "CT", "CHI": "CT", "GB": "CT", "MIN": "CT",
    "NO": "CT", "TEN": "CT",
}  # everyone else is ET

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  game_id TEXT PRIMARY KEY, season INTEGER, game_type TEXT, week INTEGER,
  gameday TEXT, weekday TEXT, gametime TEXT,
  away_team TEXT, home_team TEXT, away_score REAL, home_score REAL,
  result REAL, total REAL, overtime INTEGER, location TEXT,
  away_rest INTEGER, home_rest INTEGER,
  away_moneyline REAL, home_moneyline REAL,
  spread_line REAL, away_spread_odds REAL, home_spread_odds REAL,
  total_line REAL, under_odds REAL, over_odds REAL,
  div_game INTEGER, roof TEXT, surface TEXT, temp REAL, wind REAL,
  away_qb_name TEXT, home_qb_name TEXT, away_coach TEXT, home_coach TEXT,
  stadium TEXT, referee TEXT, away_qb_id TEXT, home_qb_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_games_season ON games(season, week);

-- Public betting splits snapshots (filled by nfl_splits.py)
CREATE TABLE IF NOT EXISTS splits (
  ts TEXT, season INTEGER, week INTEGER, game_id TEXT, source TEXT,
  market TEXT, side TEXT, line REAL, odds INTEGER,
  bets_pct REAL, money_pct REAL
);
CREATE INDEX IF NOT EXISTS ix_splits_game ON splits(game_id, market, ts);

-- Hand-collected public-fade plays (Weeks 1-3 backtest from news articles,
-- plus live plays). One row per play.
CREATE TABLE IF NOT EXISTS fade_plays (
  season INTEGER, week INTEGER, game_id TEXT, market TEXT,
  public_side TEXT, line REAL, bets_pct REAL, money_pct REAL,
  source TEXT, fade_side TEXT, result TEXT, note TEXT, live_bet INTEGER
);
"""

KEEP = [c for c in """game_id season game_type week gameday weekday gametime
away_team home_team away_score home_score result total overtime location
away_rest home_rest away_moneyline home_moneyline spread_line
away_spread_odds home_spread_odds total_line under_odds over_odds div_game
roof surface temp wind away_qb_name home_qb_name away_coach home_coach
stadium referee away_qb_id home_qb_id""".split()]


def load_games(csv_text: str) -> pd.DataFrame:
    g = pd.read_csv(io.StringIO(csv_text))
    g = g[KEEP].copy()
    for col in ("away_team", "home_team"):
        g[col] = g[col].replace(FRANCHISE)
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="local games.csv instead of downloading")
    args = ap.parse_args()

    if args.csv:
        text = Path(args.csv).read_text()
    else:
        import requests
        r = requests.get(GAMES_URL, timeout=60)
        r.raise_for_status()
        text = r.text

    g = load_games(text)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.execute("DELETE FROM games")
    g.to_sql("games", con, if_exists="append", index=False)
    con.commit()
    n = con.execute("SELECT COUNT(*), MIN(season), MAX(season), "
                    "SUM(result IS NOT NULL) FROM games").fetchone()
    con.close()
    print(f"Loaded {n[0]} games ({n[1]}-{n[2]}), {n[3]} with final scores -> {DB_PATH}")


if __name__ == "__main__":
    sys.exit(main())
