# Line Room (self-hosted)

NFL betting trends, public splits and the fade-the-public test, running on your own machine.

## What's in here

| File | What it does |
|---|---|
| `ingest.py` | Downloads every NFL game since 1999 (scores, closing lines, weather, rest) from nflverse into `nfl.db` |
| `trends.py` | Backtests 34 trends against that history and writes `site/data/trends.json` |
| `seed_fades.py` | Loads the 2026 fade-the-public plays (Weeks 1-4) and grades them |
| `props.py` | Builds the player prop streak board with next-game hit rates (`site/data/props.json`) |
| `model.py` | Power ratings + game projections (score, spread, total, team stats) with walk-forward backtest |
| `injuries.py` | Official injury report + depth charts: expected starting QBs and key players out (`site/data/injuries.json`). Run with --refresh |
| `qb.py` | Walk-forward QB ratings used by the model |
| `kalshi.py` | Kalshi player prop prices ("X or more" contracts, price = win chance) into `site/data/kalshi.json`. No API key needed |
| `.github/workflows/kalshi.yml` | Runs `kalshi.py` every 30 minutes Thu-Mon on GitHub, so prices stay fresh with your computer off |
| `people.py` | Referee and head coach records with a persistence test |
| `splits.py` | Scrapes DraftKings + Scores and Odds public splits into `nfl.db` and `site/data/splits.json` |
| `nfl_splits.py` | The scraper code `splits.py` uses (also works on its own) |
| `site/index.html` | The website. Static: just serve the `site/` folder |
| `nfl.db` | SQLite database (games, splits snapshots, fade plays) |

## Setup

Use a personal machine, not a work box.

```bash
python3 -m pip install pandas numpy requests beautifulsoup4
python3 ingest.py        # ~7,500 games, takes a few seconds
python3 seed_fades.py
python3 trends.py
python3 props.py         # about 2 minutes
python3 injuries.py --refresh
python3 model.py
python3 people.py
python3 splits.py
python3 kalshi.py
cd site && python3 -m http.server 8080   # then open http://<machine>:8080
```

## Keep it updated (crontab -e)

```cron
# Public splits every 30 minutes during the season
*/30 * * * * cd /path/to/nfl-trends && python3 splits.py >> logs.txt 2>&1
# Scores and lines refresh + trend rebuild, every morning at 6
*/30 * * * 0,1,4,5,6 cd /path/to/nfl-trends && python3 kalshi.py >> logs.txt 2>&1
0 6 * * * cd /path/to/nfl-trends && python3 ingest.py && python3 trends.py >> logs.txt 2>&1
```

To share with friends outside your network, put the `site/` folder behind a small web server
(Caddy or nginx) with a domain, or use Tailscale Funnel / Cloudflare Tunnel.

## Notes

- Lines on the site are nflverse market lines. Check your own book before betting.
- The Claude-hosted version also has a bet log; this static copy shows splits and trends only.
- If `splits.py` reports "No DraftKings games parsed", DraftKings changed its page layout.
  Run `python3 nfl_splits.py --dump` and send the saved HTML to Claude to fix the parser.
- Records assume -110. Teaser legs assume a 2-team, 6-point teaser at -120 (73.9% per leg to break even).
