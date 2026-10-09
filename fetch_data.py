#!/usr/bin/env python3
"""
Download the nflverse files the pipeline needs (free, from GitHub releases).

  data/players/w{year}.csv.gz   weekly player stats, 2001 to now
  data/team/t{year}.csv.gz      weekly team stats, 1999 to now
  data/snaps/s{year}.csv.gz     snap counts (offense %), 2013 to now
  data/injuries/*.csv           this season's injury reports and depth charts (injuries.py --refresh)

Past seasons are only downloaded once; the current season is always refreshed.
Usage: python3 fetch_data.py
"""
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
BASE = "https://github.com/nflverse/nflverse-data/releases/download"
NOW = datetime.now()
SEASON = NOW.year if NOW.month >= 8 else NOW.year - 1

JOBS = [
    ("players", "w", 2001, f"{BASE}/stats_player/stats_player_week_{{y}}.csv.gz"),
    ("team", "t", 1999, f"{BASE}/stats_team/stats_team_week_{{y}}.csv.gz"),
    ("snaps", "s", 2013, f"{BASE}/snap_counts/snap_counts_{{y}}.csv.gz"),
]


def get(url, dest):
    r = requests.get(url, timeout=180)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    dest.write_bytes(r.content)
    return True


def main():
    for folder, prefix, first, url in JOBS:
        d = HERE / "data" / folder
        d.mkdir(parents=True, exist_ok=True)
        got = 0
        for y in range(first, SEASON + 1):
            dest = d / f"{prefix}{y}.csv.gz"
            if dest.exists() and y < SEASON:
                continue
            if get(url.format(y=y), dest):
                got += 1
        print(f"{folder}: {got} files downloaded")


if __name__ == "__main__":
    main()
