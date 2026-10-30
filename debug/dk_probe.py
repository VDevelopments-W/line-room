"""One-off: look at the DraftKings Oregon (dkusor) NFL feed structure. Writes debug/dk_probe/*.json."""
import json, sys, time
from pathlib import Path
import requests
B = "https://sportsbook-nash.draftkings.com/api/sportscontent/dkusor/v1/leagues/88808"
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36", "Accept": "application/json"}
out = Path("debug/dk_probe"); out.mkdir(parents=True, exist_ok=True)
def get(u):
    r = requests.get(u, headers=H, timeout=30); print(r.status_code, u, len(r.content)); r.raise_for_status(); return r.json()
lg = get(B)
(out / "league.json").write_text(json.dumps(lg)[:400000])
print("keys", list(lg))
for k, v in lg.items():
    if isinstance(v, list): print(k, len(v), json.dumps(v[:2])[:600])
cats = lg.get("categories") or []
for c in cats:
    print("CAT", c.get("id"), c.get("name"))
for c in cats:
    n = (c.get("name") or "").lower()
    if any(w in n for w in ("pass", "rush", "rec", "player")):
        d = get(f"{B}/categories/{c['id']}"); time.sleep(0.5)
        (out / f"cat_{c['id']}.json").write_text(json.dumps(d)[:600000])
        for s in d.get("subcategories", []): print("  SUB", c["id"], s.get("id"), s.get("name"))
        for k in ("markets", "selections"):
            print("  ", k, len(d.get(k, [])), json.dumps(d.get(k, [])[:2])[:900])
        for s in d.get("subcategories", [])[:12]:
            try:
                dd = get(f"{B}/categories/{c['id']}/subcategories/{s['id']}"); time.sleep(0.5)
                (out / f"sub_{c['id']}_{s['id']}.json").write_text(json.dumps(dd)[:600000])
                print("    sub", s.get("name"), "markets", len(dd.get("markets", [])), json.dumps(dd.get("markets", [])[:1])[:500], json.dumps(dd.get("selections", [])[:2])[:700])
            except Exception as e: print("    fail", e)
