#!/usr/bin/env python3
"""
One-off: backfill DraftKings public splits for games we didn't capture live (Weeks 1-4),
using Internet Archive (Wayback Machine) snapshots of the DraftKings Network splits page.

For each past game with no DraftKings rows, find the latest archived copy of the splits page
taken before kickoff that lists the game, parse it with the same parser the live job uses,
and put those rows into the game's backfill snapshot in data/splits_history/.

Writes research/dk_backfill.txt (what was found, from which snapshot, how long before kickoff).
Runs on GitHub Actions (the archive is reachable there). Usage: python3 backfill_dk.py
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import nfl_splits as ns
from collect_splits import HIST, TO_NFLVERSE, kickoff_utc, side_rows

HERE = Path(__file__).resolve().parent
CDX = "https://web.archive.org/cdx/search/cdx"
UA = {"User-Agent": "line-room backfill (personal project)"}
MAX_FETCH = 400


def snapshots(start, end):
    seen, out = set(), []
    for url in ("dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/",
                "dknetwork.draftkings.com/draftkings-sportsbook-betting-splits"):
        for attempt in range(3):
            try:
                r = requests.get(CDX, params={"url": url, "matchType": "prefix", "from": start, "to": end,
                                              "output": "json", "filter": "statuscode:200"}, headers=UA, timeout=90)
                r.raise_for_status()
                rows = r.json()
                break
            except Exception as e:  # noqa: BLE001
                print(f"CDX retry {attempt}: {e}")
                time.sleep(10)
        else:
            continue
        for row in rows[1:]:
            ts, orig = row[1], row[2]
            if (ts, orig) not in seen:
                seen.add((ts, orig))
                out.append((ts, orig))
    return sorted(out, reverse=True)  # newest first


def main():
    tr = json.loads((HERE / "site" / "data" / "trends.json").read_text())
    need = {}
    for g in tr["season_games"]:
        if g.get("home_score") is None:
            continue
        f = HIST / f"{g['game_id']}.json"
        h = json.loads(f.read_text()) if f.exists() else None
        if h and h["snaps"] and any(r["source"] == "dk" for s in h["snaps"] if s["ts"] != "backfill" for r in s["rows"]):
            continue  # captured live
        need[(g["away"], g["home"])] = g
    if not need:
        print("Nothing to backfill")
        return
    season = tr["season"]
    last = max(g["gameday"] for g in need.values()).replace("-", "")
    snaps = snapshots(f"{season}0825", last + "235959")
    print(f"{len(need)} games need DraftKings rows; {len(snaps)} archived snapshots")
    found = {}
    fetched = 0
    for ts, orig in snaps:
        if fetched >= MAX_FETCH or len(found) == len(need):
            break
        when = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        # only useful if some still-missing game kicks off after this snapshot within 8 days
        live = [k for k, g in need.items() if k not in found and 0 < (kickoff_utc(g) - when).total_seconds() < 8 * 86400]
        if not live:
            continue
        try:
            r = requests.get(f"https://web.archive.org/web/{ts}id_/{orig}", headers=UA, timeout=60)
            fetched += 1
            if r.status_code != 200:
                continue
            games = ns.parse_dk(r.text)
        except Exception as e:  # noqa: BLE001
            print(f"  {ts} failed: {e}")
            time.sleep(5)
            continue
        hits = 0
        for dg in games:
            key = (TO_NFLVERSE.get(ns.team_abbr(dg["away"]), ns.team_abbr(dg["away"])),
                   TO_NFLVERSE.get(ns.team_abbr(dg["home"]), ns.team_abbr(dg["home"])))
            if key in live and key not in found:
                rows = side_rows(dg)
                if rows:
                    found[key] = (ts, rows)
                    hits += 1
        print(f"  {ts} {orig[-60:]}: {len(games)} games parsed, {hits} new")
        time.sleep(1.5)

    lines = [f"DraftKings backfill from Internet Archive snapshots, run {datetime.now(timezone.utc):%Y-%m-%d %H:%M}Z",
             f"{len(found)} of {len(need)} games found ({fetched} snapshots fetched)", ""]
    for key, g in sorted(need.items(), key=lambda kv: kv[1]["game_id"]):
        gid = g["game_id"]
        if key not in found:
            lines.append(f"{gid}: not archived")
            continue
        ts, rows = found[key]
        when = datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        hrs = (kickoff_utc(g) - when).total_seconds() / 3600
        f = HIST / f"{gid}.json"
        h = json.loads(f.read_text()) if f.exists() else {"game_id": gid, "kickoff": None, "backfill": True, "snaps": []}
        bf = next((s for s in h["snaps"] if s["ts"] == "backfill"), None)
        if bf is None:
            bf = {"ts": "backfill", "rows": []}
            h["snaps"].insert(0, bf)
        bf["rows"] = [r for r in bf["rows"] if r["source"] != "dk"] + rows
        bf["dk_snapshot"] = ts
        bf["dk_hours_before"] = round(hrs, 1)
        f.write_text(json.dumps(h, separators=(",", ":")))
        pct = ", ".join(f"{r['market']} {r['side']} {r['bets']:.0f}%" for r in rows if r["bets"] is not None and r["bets"] >= 70)
        lines.append(f"{gid}: snapshot {ts} ({hrs:.0f}h before kickoff){'; 70%+: ' + pct if pct else ''}")
    (HERE / "research" / "dk_backfill.txt").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
