#!/usr/bin/env python3
"""
Prepare the Claude-hosted site's database writes from the repo's latest data.

  splits/<game_id>      latest public splits (DraftKings + Scores and Odds)
  splitshist/<game_id>  every snapshot this week, for line movement
  kalshi/<game_id>      Kalshi prop prices

Usage: python3 sync_site.py [versions.json]
  versions.json (optional) maps "collection/doc_id" -> current version, from an ArtifactData list.
Writes one file per doc under sync_out/ and prints the ArtifactData batch "writes" list (JSON),
split into chunks of 50 (one line per chunk). Only this week's games are included.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
D = HERE / "site" / "data"
OUTDIR = HERE / "sync_out"


def main():
    versions = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else {}
    tr = json.loads((D / "trends.json").read_text())
    wk = tr["next_week"]
    ids = {g["game_id"] for g in tr["week_games"]} | {g["game_id"] for g in tr["season_games"] if g.get("week") == wk}
    OUTDIR.mkdir(exist_ok=True)
    for f in OUTDIR.glob("*.json"):
        f.unlink()
    docs = {}
    sp = json.loads((D / "splits.json").read_text()) if (D / "splits.json").exists() else {}
    for gid, v in sp.items():
        if gid in ids:
            docs[("splits", gid)] = v
    for gid in ids:
        h = HERE / "data" / "splits_history" / f"{gid}.json"
        if h.exists():
            snaps = json.loads(h.read_text())["snaps"]
            docs[("splitshist", gid)] = {"snaps": [s for s in snaps if s["ts"] != "backfill"][-80:]}
    k = json.loads((D / "kalshi.json").read_text()) if (D / "kalshi.json").exists() else {"games": {}}
    for gid, rows in k.get("games", {}).items():
        if gid in ids:
            docs[("kalshi", gid)] = {"updated": k.get("generated"),
                                     "rows": [{x: r.get(x) for x in ("player", "stat", "line", "bid", "ask", "last", "volume")} for r in rows]}
    writes = []
    for (coll, gid), data in sorted(docs.items()):
        if coll == "splitshist" and not data["snaps"]:
            continue
        f = OUTDIR / f"{coll}__{gid}.json"
        f.write_text(json.dumps(data, separators=(",", ":")))
        w = {"op": "set", "collection": coll, "doc_id": gid, "file_path": str(f)}
        if f"{coll}/{gid}" in versions:
            w["if_version"] = versions[f"{coll}/{gid}"]
        writes.append(w)
    for i in range(0, len(writes), 50):
        print(json.dumps(writes[i:i + 50]))
    print(f"{len(writes)} docs", file=sys.stderr)


if __name__ == "__main__":
    main()
