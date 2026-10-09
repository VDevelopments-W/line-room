#!/usr/bin/env python3
"""
Research only (not used by the models): actual weather at kickoff for every outdoor game 2012-2025,
from the Open-Meteo historical archive, so we can measure how much rain matters.

Writes research/rain_games.csv: game_id, precip_in, rain_in, snow_in, wet_hours, wind_mph, temp_f
(precip/rain/snow summed over the 3 game hours; wet_hours = game hours with 0.01"+ of precipitation).
Runs on GitHub Actions (the archive isn't reachable from the dev workspace).
"""
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "research" / "rain_games.csv"
API = "https://archive-api.open-meteo.com/v1/archive"

LL = {  # stadium name (substring) -> lat, lon
    "M&T Bank": (39.2780, -76.6227), "New Era": (42.7738, -78.7870), "Ralph Wilson": (42.7738, -78.7870),
    "Bank of America": (35.2258, -80.8528), "Soldier": (41.8623, -87.6167), "Paul Brown": (39.0955, -84.5161),
    "Paycor": (39.0955, -84.5161), "Cleveland Browns": (41.5061, -81.6995), "FirstEnergy": (41.5061, -81.6995),
    "Mile High": (39.7439, -105.0201), "Lambeau": (44.5013, -88.0622), "EverBank": (30.3239, -81.6373),
    "TIAA": (30.3239, -81.6373), "Arrowhead": (39.0489, -94.4839), "Memorial Coliseum": (34.0141, -118.2879),
    "Qualcomm": (32.7831, -117.1196), "StubHub": (33.8644, -118.2611), "O.co": (37.7516, -122.2005),
    "Oakland-Alameda": (37.7516, -122.2005), "Ring Central": (37.7516, -122.2005), "Hard Rock": (25.9580, -80.2389),
    "Sun Life": (25.9580, -80.2389), "TCF": (44.9765, -93.2246), "Gillette": (42.0909, -71.2643),
    "MetLife": (40.8135, -74.0745), "Lincoln Financial": (39.9008, -75.1675), "Acrisure": (40.4468, -80.0158),
    "Heinz": (40.4468, -80.0158), "CenturyLink": (47.5952, -122.3316), "Lumen": (47.5952, -122.3316),
    "Candlestick": (37.7136, -122.3863), "Levi": (37.4030, -121.9700), "Raymond James": (27.9759, -82.5033),
    "LP Field": (36.1665, -86.7713), "Nissan": (36.1665, -86.7713), "FedEx": (38.9077, -76.8645),
    "State Farm": (33.5276, -112.2626), "University of Phoenix": (33.5276, -112.2626), "Mercedes-Benz": (33.7554, -84.4008),
    "AT&T": (32.7473, -97.0945), "Cowboys Stadium": (32.7473, -97.0945), "NRG": (29.6847, -95.4107),
    "Lucas Oil": (39.7601, -86.1639), "Tottenham": (51.6043, -0.0664), "Wembley": (51.5560, -0.2796),
    "Twickenham": (51.4560, -0.3415), "Allianz": (48.2188, 11.6247), "Deutsche Bank": (50.0686, 8.6455),
    "Azteca": (19.3029, -99.1505), "Corinthians": (-23.5453, -46.4742),
}


def coords(stadium):
    for k, v in LL.items():
        if k.lower() in (stadium or "").lower():
            return v
    return None


def main():
    con = sqlite3.connect(HERE / "nfl.db")
    g = pd.read_sql("SELECT game_id, season, gameday, gametime, stadium, roof FROM games "
                    "WHERE season BETWEEN 2012 AND 2025 AND roof IN ('outdoors','open') AND result IS NOT NULL", con)
    con.close()
    g["ll"] = g.stadium.map(coords)
    missing = g[g.ll.isna()].stadium.unique()
    if len(missing):
        print("No coordinates for:", list(missing), file=sys.stderr)
    g = g[g.ll.notna()].copy()
    ny = ZoneInfo("America/New_York")
    g["ko"] = [datetime.fromisoformat(f"{d}T{t or '13:00'}").replace(tzinfo=ny).astimezone(timezone.utc) for d, t in zip(g.gameday, g.gametime)]
    rows = []
    for (ll, season), grp in g.groupby(["ll", "season"]):
        start = (grp.ko.min() - timedelta(days=1)).date().isoformat()
        end = (grp.ko.max() + timedelta(days=1)).date().isoformat()
        for attempt in range(4):
            try:
                r = requests.get(API, params={"latitude": ll[0], "longitude": ll[1], "start_date": start, "end_date": end,
                                              "hourly": "precipitation,rain,snowfall,wind_speed_10m,temperature_2m",
                                              "timezone": "UTC", "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                                              "precipitation_unit": "inch"}, timeout=60)
                r.raise_for_status()
                h = r.json()["hourly"]
                break
            except Exception as e:  # noqa: BLE001
                print(f"retry {ll} {season}: {e}", file=sys.stderr)
                time.sleep(10 * (attempt + 1))
        else:
            continue
        idx = {t: i for i, t in enumerate(h["time"])}
        for x in grp.itertuples():
            k0 = x.ko.replace(minute=0)
            ii = [idx.get((k0 + timedelta(hours=j)).strftime("%Y-%m-%dT%H:%M")) for j in range(3)]
            ii = [i for i in ii if i is not None]
            if not ii:
                continue
            s = lambda key: sum(h[key][i] or 0 for i in ii)  # noqa: E731
            rows.append({"game_id": x.game_id, "precip_in": round(s("precipitation"), 3), "rain_in": round(s("rain"), 3),
                         "snow_in": round(s("snowfall"), 3), "wet_hours": sum((h["precipitation"][i] or 0) >= 0.01 for i in ii),
                         "wind_mph": round(s("wind_speed_10m") / len(ii), 1), "temp_f": round(s("temperature_2m") / len(ii), 1)})
        time.sleep(0.3)
    pd.DataFrame(rows).to_csv(OUT, index=False)
    print(f"{len(rows)} outdoor games with archive weather -> {OUT}")


if __name__ == "__main__":
    main()
