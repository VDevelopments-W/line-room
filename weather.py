#!/usr/bin/env python3
"""
Game-day weather for this week's games (wind, temperature, rain/snow).

History (for fitting the models) comes from nflverse's games file: roof, temp, wind.
Forecasts come from Open-Meteo (free, no key), at each stadium's location for the kickoff hour.
Domes and closed roofs get no weather.

Usage: python3 weather.py   -> site/data/weather.json
  {generated, games: {game_id: {roof, wind, gust, temp, precip_prob, precip, snow, label}}}
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

from collect_splits import kickoff_utc

HERE = Path(__file__).resolve().parent
OUT = HERE / "site" / "data" / "weather.json"
API = "https://api.open-meteo.com/v1/forecast"

# home stadiums (lat, lon). Indoor teams are listed too in case nflverse marks a roof "open".
STADIUM = {
    "ARI": (33.5276, -112.2626), "ATL": (33.7554, -84.4008), "BAL": (39.2780, -76.6227), "BUF": (42.7738, -78.7870),
    "CAR": (35.2258, -80.8528), "CHI": (41.8623, -87.6167), "CIN": (39.0955, -84.5161), "CLE": (41.5061, -81.6995),
    "DAL": (32.7473, -97.0945), "DEN": (39.7439, -105.0201), "DET": (42.3400, -83.0456), "GB": (44.5013, -88.0622),
    "HOU": (29.6847, -95.4107), "IND": (39.7601, -86.1639), "JAX": (30.3239, -81.6373), "KC": (39.0489, -94.4839),
    "LA": (33.9535, -118.3392), "LAC": (33.9535, -118.3392), "LV": (36.0909, -115.1833), "MIA": (25.9580, -80.2389),
    "MIN": (44.9737, -93.2575), "NE": (42.0909, -71.2643), "NO": (29.9511, -90.0812), "NYG": (40.8135, -74.0745),
    "NYJ": (40.8135, -74.0745), "PHI": (39.9008, -75.1675), "PIT": (40.4468, -80.0158), "SEA": (47.5952, -122.3316),
    "SF": (37.4030, -121.9700), "TB": (27.9759, -82.5033), "TEN": (36.1665, -86.7713), "WAS": (38.9077, -76.8645),
}
# neutral-site games, matched on nflverse's stadium name
NEUTRAL = {
    "Tottenham": (51.6043, -0.0664), "Wembley": (51.5560, -0.2796), "Allianz": (48.2188, 11.6247),
    "Deutsche Bank": (50.0686, 8.6455), "Olympiastadion": (52.5147, 13.2395), "Azteca": (19.3029, -99.1505),
    "Bernab": (40.4531, -3.6883), "Corinthians": (-23.5453, -46.4742), "Maracan": (-22.9122, -43.2302),
    "Melbourne": (-37.8200, 144.9834), "Croke": (53.3607, -6.2512), "Stade de France": (48.9245, 2.3602),
}
INDOOR = {"dome", "closed"}


def week_games():
    tr = json.loads((HERE / "site" / "data" / "trends.json").read_text())
    games = {g["game_id"]: g for g in tr["season_games"] if g.get("week") == tr["next_week"]}
    games.update({g["game_id"]: g for g in tr["week_games"]})
    meta = {}
    db = HERE / "nfl.db"
    if db.exists():
        con = sqlite3.connect(db)
        for gid, roof, stadium, loc in con.execute("SELECT game_id, roof, stadium, location FROM games WHERE game_id IN (%s)"
                                                   % ",".join("?" * len(games)), list(games)):
            meta[gid] = (roof, stadium or "", loc)
        con.close()
    return games, meta


def where(g, meta):
    roof, stadium, loc = meta.get(g["game_id"], (None, "", "Home"))
    if loc == "Neutral":
        for k, ll in NEUTRAL.items():
            if k.lower() in stadium.lower():
                return roof, ll
        return roof, None
    return roof, STADIUM.get(g["home"])


def label(w):
    if w.get("roof") in INDOOR:
        return "Indoors"
    if w.get("wind") is None:
        return ""
    parts = [f"{w['temp']:.0f}°F", f"wind {w['wind']:.0f} mph"]
    if (w.get("snow") or 0) > 0.05:
        parts.append("snow")
    elif (w.get("precip_prob") or 0) >= 50:
        parts.append(f"rain {w['precip_prob']:.0f}%")
    return ", ".join(parts)


def main():
    games, meta = week_games()
    out = {}
    for gid, g in games.items():
        roof, ll = where(g, meta)
        w = {"roof": roof}
        ko = kickoff_utc(g)
        if roof not in INDOOR and ll and ko > datetime.now(timezone.utc):
            try:
                r = requests.get(API, params={"latitude": ll[0], "longitude": ll[1], "timezone": "UTC", "forecast_days": 16,
                                              "hourly": "temperature_2m,wind_speed_10m,wind_gusts_10m,precipitation_probability,precipitation,snowfall",
                                              "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "precipitation_unit": "inch"},
                                 timeout=30)
                r.raise_for_status()
                h = r.json()["hourly"]
                # the 3 hours the game is played: average them
                idx = [i for i, t in enumerate(h["time"]) if 0 <= (datetime.fromisoformat(t).replace(tzinfo=timezone.utc) - ko).total_seconds() < 3 * 3600]
                if idx:
                    avg = lambda k: None if any(h[k][i] is None for i in idx) else sum(h[k][i] for i in idx) / len(idx)  # noqa: E731
                    w.update({"temp": avg("temperature_2m"), "wind": avg("wind_speed_10m"), "gust": max(h["wind_gusts_10m"][i] or 0 for i in idx),
                              "precip_prob": max(h["precipitation_probability"][i] or 0 for i in idx),
                              "precip": sum(h["precipitation"][i] or 0 for i in idx), "snow": sum(h["snowfall"][i] or 0 for i in idx)})
                    w = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in w.items()}
            except Exception as e:  # noqa: BLE001
                print(f"  {gid}: forecast failed ({e})")
        w["label"] = label(w)
        out[gid] = w
    old = json.loads(OUT.read_text()).get("games", {}) if OUT.exists() else {}
    for gid, w in old.items():  # keep the last forecast for games already started
        if gid in out and out[gid].get("wind") is None and w.get("wind") is not None:
            out[gid] = w
    OUT.write_text(json.dumps({"generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "games": out}))
    print(f"Weather: {sum(1 for w in out.values() if w.get('wind') is not None)} outdoor forecasts, "
          f"{sum(1 for w in out.values() if w.get('roof') in INDOOR)} indoors -> {OUT}")
    for gid, w in sorted(out.items()):
        if (w.get("wind") or 0) >= 15 or (w.get("precip_prob") or 0) >= 60 or (w.get("snow") or 0) > 0.05:
            print(f"  {gid}: {w['label']}")


if __name__ == "__main__":
    main()
