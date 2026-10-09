#!/usr/bin/env python3
"""
NFL public betting splits tracker (free).

Sources:
  - DraftKings Network "Betting Splits" page: % of bets and % of handle for
    spread, total and moneyline, all DK states combined. Primary source,
    also provides the lines.
  - Scores and Odds consensus page: multi-book % of bets / % of money
    (book not named). Shown side by side as a second opinion.

Each run:
  1. Scrapes both sources for upcoming NFL games
  2. Saves a timestamped snapshot to SQLite (splits.db) so you get history
  3. Grades finished games (final scores from ESPN's public scoreboard) and
     tracks how fading 80%+ public sides on spread/total would have done
  4. Rebuilds splits.html: current splits, movement, flags, fade record

Setup:
  pip install requests beautifulsoup4
  python3 nfl_splits.py              # scrape + grade + dashboard
  python3 nfl_splits.py --report     # grade + dashboard only (no scrape)
  python3 nfl_splits.py --dump       # also save raw HTML (send me if parsing breaks)

Run on a schedule so the last snapshot before kickoff is close to kickoff:
  */30 * * * * cd /path/to/folder && /usr/bin/python3 nfl_splits.py >> splits.log 2>&1
"""

import argparse
import html
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone, date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

DK_URL = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
SAO_URL = "https://www.scoresandodds.com/nfl/consensus-picks"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NFL_GROUP = "88808"          # DK event group id for NFL
DATE_RANGE = "n7days"        # today | tomorrow | n7days | n30days
MAX_PAGES = 10
HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "splits.db"
HTML_PATH = HERE / "splits.html"

FADE_AT = 80        # bets% at or above this on spread/total = fade candidate
SHARP_GAP = 15      # handle% minus bets% at or above this = money-heavy side
PUBLIC_HEAVY = 70   # bets% at or above this = lopsided public side

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml",
}

MARKETS = {"Moneyline", "Spread", "Total"}
LABELS = {"Odds", "% Handle", "% Bets"}
ODDS_RE = re.compile(r"^[+\-−]?\d{2,5}$|^EVEN$", re.I)
PCT_RE = re.compile(r"^(\d{1,3})%$")
DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2}),\s*\d{1,2}:\d{2}\s*[AP]M$", re.I)
EVENT_RE = re.compile(r"/event/(\d+)")
LINE_RE = re.compile(r"\s([+\-−]?\d+(?:\.\d+)?)$")

# Nicknames whose DK city prefix is ambiguous ("LA", "NY")
NICK_ABBR = {"Rams": "LAR", "Chargers": "LAC", "Jets": "NYJ", "Giants": "NYG"}
ABBR_ALIAS = {"WSH": "WAS", "JAC": "JAX", "LA": "LAR", "LV": "LV", "KAN": "KC"}


def team_abbr(dk_team: str) -> str:
    """'IND Colts' -> 'IND', 'LA Rams' -> 'LAR'."""
    parts = dk_team.split()
    return NICK_ABBR.get(parts[-1], parts[0].upper())


def nickname(dk_team: str) -> str:
    return dk_team.split()[-1]


# ----------------------------------------------------------------- DraftKings

def fetch(url, params=None) -> str:
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.text


def parse_odds(s: str):
    s = s.replace("−", "-").upper()
    return 100 if s == "EVEN" else int(s)


def parse_dk(page_html: str) -> list[dict]:
    """Parse a DK splits page. Works off the visible text sequence (title,
    kickoff, then market blocks of selection/odds/handle%/bets%), so it
    doesn't break when DK renames CSS classes."""
    soup = BeautifulSoup(page_html, "html.parser")
    titles = {}
    for a in soup.find_all("a", href=EVENT_RE):
        txt = a.get_text(" ", strip=True)
        if " @ " in txt and "outcomes=" not in a["href"]:
            titles[txt] = EVENT_RE.search(a["href"]).group(1)

    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    games, game, market, i = [], None, None, 0
    while i < len(lines):
        ln = lines[i]
        if ln in titles:
            away, home = [t.strip() for t in ln.split(" @ ", 1)]
            game = {"event_id": titles[ln], "away": away, "home": home,
                    "kickoff": "", "sides": []}
            games.append(game)
            market = None
        elif game is not None and DATE_RE.match(ln) and not game["kickoff"]:
            game["kickoff"] = ln
        elif game is not None and ln in MARKETS:
            market = ln
        elif game is not None and market and ln not in LABELS:
            chunk = lines[i:i + 4]
            if (len(chunk) == 4 and ODDS_RE.match(chunk[1])
                    and PCT_RE.match(chunk[2]) and PCT_RE.match(chunk[3])):
                sel = chunk[0]
                m = LINE_RE.search(sel) if market != "Moneyline" else None
                game["sides"].append({
                    "source": "dk", "market": market, "selection": sel,
                    "line": float(m.group(1).replace("−", "-")) if m else None,
                    "odds": parse_odds(chunk[1]),
                    "handle_pct": int(PCT_RE.match(chunk[2]).group(1)),
                    "bets_pct": int(PCT_RE.match(chunk[3]).group(1)),
                })
                i += 4
                continue
        i += 1
    return [g for g in games if g["sides"]]


def scrape_dk(dump=False) -> list[dict]:
    seen, out = set(), []
    for page in range(1, MAX_PAGES + 1):
        params = {"tb_eg": NFL_GROUP, "tb_edate": DATE_RANGE, "tb_emt": "0"}
        if page > 1:
            params["tb_page"] = str(page)
        page_html = fetch(DK_URL, params)
        if dump:
            (HERE / f"dump_dk_{page}.html").write_text(page_html, encoding="utf-8")
        new = [g for g in parse_dk(page_html) if g["event_id"] not in seen]
        if not new:
            break
        for g in new:
            seen.add(g["event_id"])
        out.extend(new)
        time.sleep(2)  # be polite
    return out


# ----------------------------------------------------------------- Scores and Odds

SIDE_TEAM_RE = re.compile(r"^([A-Z]{2,4})\b")
SIDE_TOTAL_RE = re.compile(r"^(over|under|o|u)(?=[\s\d.]|$)", re.I)
SPREAD_TOKEN_RE = re.compile(r"(?<![\d.])[+\-−]\d+(?:\.5)?(?![\d%])")
MARKET_WORDS = {"moneyline": "Moneyline", "ml": "Moneyline", "spread": "Spread",
                "ats": "Spread", "total": "Total", "over/under": "Total",
                "o/u": "Total"}


def parse_sao(page_html: str) -> list[dict]:
    """Best-effort parser for the Scores and Odds consensus page. Each market
    block reads as: <side A> '% of Bets' <side B> betsA% betsB% moneyA% moneyB%
    '% of Money'. Blocks with percentages that don't add to ~100 are skipped."""
    soup = BeautifulSoup(page_html, "html.parser")
    lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]
    blocks = []
    for i, ln in enumerate(lines):
        if ln.lower() != "% of bets" or i == 0 or i + 1 >= len(lines):
            continue
        a, b = lines[i - 1], lines[i + 1]
        pcts = []
        for ln2 in lines[i + 2:i + 14]:
            if ln2.lower() == "% of bets":
                break
            m = PCT_RE.match(ln2)
            if m:
                pcts.append(int(m.group(1)))
            if len(pcts) == 4:
                break
        if len(pcts) < 4 or not 97 <= pcts[0] + pcts[1] <= 103:
            continue
        ctx = " ".join(lines[max(0, i - 8):i - 1]).lower()
        label = next((v for k, v in MARKET_WORDS.items() if re.search(rf"\b{re.escape(k)}\b", ctx)), None)
        blocks.append({"a": a, "b": b, "pcts": pcts, "label": label,
                       "has_line": bool(SPREAD_TOKEN_RE.search(f"{a} {b}"))})

    games, cur = [], None
    for blk in blocks:
        if SIDE_TOTAL_RE.match(blk["a"]) or blk["label"] == "Total":
            if cur:
                cur["blocks"].append(("Total", blk))
            continue
        ma, mb = SIDE_TEAM_RE.match(blk["a"]), SIDE_TEAM_RE.match(blk["b"])
        if not (ma and mb):
            continue
        pair = (ABBR_ALIAS.get(ma.group(1), ma.group(1)),
                ABBR_ALIAS.get(mb.group(1), mb.group(1)))
        if not cur or cur["pair"] != pair:
            cur = {"pair": pair, "blocks": []}
            games.append(cur)
        team_blocks = sum(1 for m, _ in cur["blocks"] if m != "Total")
        market = blk["label"] or ("Spread" if blk["has_line"] else
                                  ("Moneyline" if team_blocks == 0 else "Spread"))
        cur["blocks"].append((market, blk))
    return games


def merge_sao(dk_games: list[dict], sao_games: list[dict]) -> int:
    """Attach Scores and Odds sides to matching DK games, keyed so they line
    up with DK selections ('IND Colts', 'Over', ...)."""
    by_pair = {g["pair"]: g for g in sao_games}
    matched = 0
    for g in dk_games:
        pa, ph = team_abbr(g["away"]), team_abbr(g["home"])
        sg = by_pair.get((pa, ph)) or by_pair.get((ph, pa))
        if not sg:
            continue
        matched += 1
        name_for = {pa: g["away"], ph: g["home"]}
        for market, blk in sg["blocks"]:
            p = blk["pcts"]
            if market == "Total":
                sides = [("Over", p[0], p[2]), ("Under", p[1], p[3])]
                if SIDE_TOTAL_RE.match(blk["a"]) and blk["a"].lower().startswith("u"):
                    sides = [("Under", p[0], p[2]), ("Over", p[1], p[3])]
            else:
                sides = [(name_for[sg["pair"][0]], p[0], p[2]),
                         (name_for[sg["pair"][1]], p[1], p[3])]
            for sel, bets, money in sides:
                g["sides"].append({"source": "sao", "market": market, "selection": sel,
                                   "line": None, "odds": None,
                                   "handle_pct": money, "bets_pct": bets})
    return matched


# ----------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
  event_id TEXT PRIMARY KEY, away TEXT, home TEXT, kickoff TEXT,
  first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  ts TEXT, event_id TEXT, market TEXT, selection TEXT, line REAL,
  odds INTEGER, handle_pct INTEGER, bets_pct INTEGER, source TEXT DEFAULT 'dk'
);
CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(event_id, market, ts);
CREATE TABLE IF NOT EXISTS results (
  event_id TEXT PRIMARY KEY, away_score INTEGER, home_score INTEGER, graded_at TEXT
);
"""


def db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    cols = [r[1] for r in con.execute("PRAGMA table_info(snapshots)")]
    if "source" not in cols:
        con.execute("ALTER TABLE snapshots ADD COLUMN source TEXT DEFAULT 'dk'")
    return con


def save(games: list[dict]) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    con = db()
    for g in games:
        con.execute(
            "INSERT INTO games VALUES (?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE "
            "SET kickoff=excluded.kickoff, last_seen=excluded.last_seen",
            (g["event_id"], g["away"], g["home"], g["kickoff"], ts, ts))
        for s in g["sides"]:
            con.execute("INSERT INTO snapshots (ts,event_id,market,selection,line,odds,"
                        "handle_pct,bets_pct,source) VALUES (?,?,?,?,?,?,?,?,?)",
                        (ts, g["event_id"], s["market"], s["selection"], s["line"],
                         s["odds"], s["handle_pct"], s["bets_pct"], s["source"]))
    con.commit()
    con.close()
    return ts


# ----------------------------------------------------------------- grading

def kickoff_date(kickoff: str, first_seen: str):
    m = DATE_RE.match(kickoff or "")
    if not m:
        return None
    seen = datetime.strptime(first_seen[:10], "%Y-%m-%d").date()
    month, day = int(m.group(1)), int(m.group(2))
    year = seen.year + (1 if month < seen.month - 6 else 0)
    return date(year, month, day)


def grade_finished(verbose=True) -> int:
    """Pull final scores from ESPN for past games that aren't graded yet."""
    con = db()
    rows = con.execute("SELECT g.* FROM games g LEFT JOIN results r USING(event_id) "
                       "WHERE r.event_id IS NULL").fetchall()
    today = datetime.now().date()
    by_date = {}
    for ev, away, home, ko, first_seen, _ in rows:
        d = kickoff_date(ko, first_seen)
        if d and d <= today:
            by_date.setdefault(d, []).append((ev, away, home))
    n = 0
    for d, games in by_date.items():
        try:
            r = requests.get(ESPN_URL, params={"dates": d.strftime("%Y%m%d")},
                             headers=HEADERS, timeout=30)
            r.raise_for_status()
            events = r.json().get("events", [])
        except Exception as e:  # noqa: BLE001
            if verbose:
                print(f"ESPN lookup failed for {d}: {e}", file=sys.stderr)
            continue
        finals = {}
        for e in events:
            comp = e["competitions"][0]
            if not comp.get("status", e.get("status", {})).get("type", {}).get("completed"):
                continue
            sc = {c["homeAway"]: (c["team"]["name"], int(c["score"])) for c in comp["competitors"]}
            finals[(sc["away"][0], sc["home"][0])] = (sc["away"][1], sc["home"][1])
        for ev, away, home in games:
            f = finals.get((nickname(away), nickname(home)))
            if f:
                con.execute("INSERT OR REPLACE INTO results VALUES (?,?,?,?)",
                            (ev, f[0], f[1], datetime.now(timezone.utc).isoformat()))
                n += 1
    con.commit()
    con.close()
    return n


def side_key(market, selection):
    """Stable key for a side even when the line moves
    ('BUF Bills -7' -> 'BUF Bills', 'Over 50.5' -> 'Over')."""
    if market == "Moneyline":
        return selection
    return LINE_RE.sub("", " " + selection).strip()


def latest_by_source(con, event_id, source):
    ts = con.execute("SELECT MAX(ts) FROM snapshots WHERE event_id=? AND source=?",
                     (event_id, source)).fetchone()[0]
    if not ts:
        return {}
    rows = con.execute("SELECT market, selection, line, bets_pct, handle_pct FROM snapshots "
                       "WHERE event_id=? AND source=? AND ts=?", (event_id, source, ts)).fetchall()
    return {(m, side_key(m, s)): {"sel": s, "line": l, "bets": b, "money": h}
            for m, s, l, b, h in rows}


def fade_record():
    """Fade every spread/total side at FADE_AT%+ of bets in the last snapshot
    before kickoff, graded at DK's line for the other side."""
    con = db()
    graded = con.execute("SELECT g.event_id, g.away, g.home, g.kickoff, r.away_score, "
                         "r.home_score FROM games g JOIN results r USING(event_id) "
                         "ORDER BY g.first_seen").fetchall()
    plays = []
    for ev, away, home, ko, a_sc, h_sc in graded:
        dk = latest_by_source(con, ev, "dk")
        for source in ("dk", "sao"):
            snap = dk if source == "dk" else latest_by_source(con, ev, "sao")
            for (market, key), s in snap.items():
                if market not in ("Spread", "Total") or s["bets"] < FADE_AT:
                    continue
                opp = [(k, v) for k, v in dk.items() if k[0] == market and k[1] != key]
                if not opp or opp[0][1]["line"] is None:
                    continue
                okey, o = opp[0]
                line = o["line"]
                if market == "Total":
                    total = a_sc + h_sc
                    diff = (total - line) if okey[1] == "Over" else (line - total)
                else:
                    mine, theirs = (a_sc, h_sc) if okey[1] == away else (h_sc, a_sc)
                    diff = mine + line - theirs
                res = "W" if diff > 0 else ("L" if diff < 0 else "P")
                plays.append({"source": source, "market": market, "game": f"{away} @ {home}",
                              "kickoff": ko, "public": f"{key} ({s['bets']}% bets)",
                              "fade": o["sel"], "score": f"{a_sc}-{h_sc}", "res": res})
    con.close()
    return plays


def tally(plays):
    w = sum(p["res"] == "W" for p in plays)
    l = sum(p["res"] == "L" for p in plays)
    pu = sum(p["res"] == "P" for p in plays)
    units = w * (100 / 110) - l          # assumes -110
    return f"{w}-{l}" + (f"-{pu}" if pu else "") + f" ({units:+.1f}u at -110)"


# ----------------------------------------------------------------- report

def load_report_data():
    con = db()
    con.row_factory = sqlite3.Row
    latest_ts = con.execute("SELECT MAX(ts) FROM snapshots").fetchone()[0]
    if not latest_ts:
        return None, []
    games = con.execute("SELECT * FROM games WHERE last_seen = ? ORDER BY rowid",
                        (latest_ts,)).fetchall()
    out = []
    for g in games:
        rows = con.execute("SELECT * FROM snapshots WHERE event_id=? ORDER BY ts",
                           (g["event_id"],)).fetchall()
        first, last, sao = {}, {}, {}
        for r in rows:
            k = (r["market"], side_key(r["market"], r["selection"]))
            if r["source"] == "sao":
                if r["ts"] == latest_ts:
                    sao[k] = r
                continue
            first.setdefault(k, r)
            if r["ts"] == latest_ts:
                last[k] = r
        n_snaps = len({r["ts"] for r in rows})
        out.append({"g": dict(g), "first": first, "last": last, "sao": sao, "n": n_snaps})
    con.close()
    return latest_ts, out


def fmt_odds(o):
    return f"+{o}" if o > 0 else str(o)


def fmt_delta(v, suffix=""):
    if v is None or abs(v) < 1e-9:
        return ""
    cls = "up" if v > 0 else "down"
    sign = "+" if v > 0 else ""
    v = int(v) if float(v).is_integer() else v
    return f'<span class="d {cls}">{sign}{v}{suffix}</span>'


def build_record_html(plays):
    if not plays:
        return ('<p class="muted">No graded games yet. The record fills in automatically '
                'as games finish (needs a snapshot taken before kickoff).</p>')
    parts = []
    for src, name in (("dk", "DraftKings"), ("sao", "Scores and Odds consensus")):
        sp = [p for p in plays if p["source"] == src]
        if not sp:
            continue
        sub = " &middot; ".join(f"{m}: {tally([p for p in sp if p['market'] == m])}"
                                for m in ("Spread", "Total") if any(p["market"] == m for p in sp))
        rows = "".join(
            f'<tr><td>{html.escape(p["game"])}</td><td>{html.escape(p["public"])}</td>'
            f'<td>{html.escape(p["fade"])}</td><td>{p["score"]}</td>'
            f'<td class="r {p["res"]}">{p["res"]}</td></tr>' for p in reversed(sp))
        parts.append(f"""<div class="rec"><b>{name}: fade {tally(sp)}</b>
<div class="muted">{sub}</div>
<details><summary>Every play</summary><table class="plays">
<thead><tr><th>Game</th><th>Public</th><th>Fade</th><th>Final</th><th></th></tr></thead>
<tbody>{rows}</tbody></table></details></div>""")
    return "".join(parts)


def build_html(latest_ts, data, plays):
    flags_html, fades_html, cards = [], [], []
    for item in data:
        g, first, last, sao = item["g"], item["first"], item["last"], item["sao"]
        title = f'{html.escape(g["away"])} @ {html.escape(g["home"])}'
        blocks = []
        for market in ("Spread", "Total", "Moneyline"):
            sides = [(k, r) for k, r in last.items() if k[0] == market]
            if not sides:
                continue
            rows = []
            for k, r in sides:
                f = first.get(k)
                c = sao.get(k)
                gap = r["handle_pct"] - r["bets_pct"]
                tags = []
                if gap >= SHARP_GAP:
                    tags.append('<span class="tag sharp">$ &gt; tix</span>')
                    flags_html.append(
                        f'<li><b>{title}</b>: {html.escape(r["selection"])} '
                        f'({market.lower()}) has {r["handle_pct"]}% of money on '
                        f'{r["bets_pct"]}% of bets</li>')
                if r["bets_pct"] >= PUBLIC_HEAVY:
                    tags.append('<span class="tag pub">public</span>')
                if market != "Moneyline" and (r["bets_pct"] >= FADE_AT or
                                              (c and c["bets_pct"] >= FADE_AT)):
                    tags.append('<span class="tag fade">fade?</span>')
                    cons = f', consensus {c["bets_pct"]}%' if c else ""
                    fades_html.append(f'<li><b>{title}</b>: {html.escape(r["selection"])} '
                                      f'has DK {r["bets_pct"]}%{cons} of bets</li>')
                line_d = (r["line"] - f["line"]) if (f and r["line"] is not None
                                                     and f["line"] is not None) else None
                cons_cell = (f'{c["bets_pct"]}%<span class="muted"> / {c["handle_pct"]}%</span>'
                             if c else '<span class="muted">-</span>')
                rows.append(f"""
<tr>
  <td class="sel">{html.escape(r["selection"])}{fmt_delta(line_d)} {''.join(tags)}</td>
  <td class="num">{fmt_odds(r["odds"])}</td>
  <td><div class="bar"><i style="width:{r["bets_pct"]}%"></i></div>
      <span class="pct">{r["bets_pct"]}%</span>{fmt_delta(r["bets_pct"] - f["bets_pct"] if f else None)}</td>
  <td><div class="bar money"><i style="width:{r["handle_pct"]}%"></i></div>
      <span class="pct">{r["handle_pct"]}%</span>{fmt_delta(r["handle_pct"] - f["handle_pct"] if f else None)}</td>
  <td class="pct cons">{cons_cell}</td>
</tr>""")
            blocks.append(f"""
<table><thead><tr><th>{market}</th><th>Odds</th><th>DK bets</th><th>DK money</th>
<th>Consensus</th></tr></thead><tbody>{''.join(rows)}</tbody></table>""")
        cards.append(f"""
<section class="card">
  <header><h2>{title}</h2><span class="ko">{html.escape(g["kickoff"])} ET
  &middot; {item["n"]} snapshot{'s' if item["n"] != 1 else ''}</span></header>
  <div class="scroll">{''.join(blocks)}</div>
</section>""")

    flags = (f'<ul class="flags">{"".join(flags_html)}</ul>' if flags_html
             else '<p class="muted">No sides where money is well ahead of tickets right now.</p>')
    fades = (f'<ul class="flags">{"".join(fades_html)}</ul>' if fades_html
             else f'<p class="muted">No spread or total at {FADE_AT}%+ of bets right now.</p>')
    updated = datetime.strptime(latest_ts, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc).astimezone().strftime("%a %b %d, %I:%M %p %Z")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NFL Betting Splits</title>
<style>
:root {{ --bg:#f6f5f2; --card:#fff; --ink:#1b1b1b; --muted:#6b6b6b; --line:#e4e2dc;
  --tix:#4a7bd0; --money:#2f9e6b; --up:#2f9e6b; --down:#c8553d; --sharp:#7a4fd0;
  --pub:#c98a1b; --fade:#c8553d; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#121212; --card:#1c1c1c; --ink:#ececec;
  --muted:#9a9a9a; --line:#2c2c2c; }} }}
* {{ box-sizing:border-box }}
body {{ margin:0; background:var(--bg); color:var(--ink);
  font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }}
main {{ max-width:1200px; margin:0 auto; padding:20px 16px 60px; }}
h1 {{ font-size:22px; margin:0 0 2px }} h3 {{ margin:18px 0 6px }}
.muted,.ko {{ color:var(--muted) }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%,400px),1fr)); gap:14px }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px }}
.card header {{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; flex-wrap:wrap }}
.scroll {{ overflow-x:auto }}
h2 {{ font-size:15px; margin:0 }} .ko {{ font-size:12px }}
table {{ width:100%; border-collapse:collapse; margin-top:10px }}
th {{ text-align:left; font-size:11px; text-transform:uppercase; letter-spacing:.04em;
  color:var(--muted); font-weight:600; padding-bottom:4px; border-bottom:1px solid var(--line) }}
td {{ padding:5px 4px 5px 0; vertical-align:middle; white-space:nowrap }}
td.sel {{ white-space:normal; padding-right:6px }} td.num {{ padding-right:8px; color:var(--muted) }}
.bar {{ display:inline-block; width:44px; height:6px; background:var(--line); border-radius:3px;
  vertical-align:middle; margin-right:5px; overflow:hidden }}
.bar i {{ display:block; height:100%; background:var(--tix) }} .bar.money i {{ background:var(--money) }}
.pct {{ font-variant-numeric:tabular-nums }}
.d {{ font-size:11px; margin-left:4px }} .d.up {{ color:var(--up) }} .d.down {{ color:var(--down) }}
.tag {{ font-size:10px; padding:1px 5px; border-radius:8px; color:#fff; margin-left:3px }}
.tag.sharp {{ background:var(--sharp) }} .tag.pub {{ background:var(--pub) }}
.tag.fade {{ background:var(--fade) }}
.flags,.rec {{ background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:12px 12px 12px 30px; margin:6px 0 12px }}
.rec {{ padding-left:14px }}
.plays td {{ white-space:normal }} .r.W {{ color:var(--up) }} .r.L {{ color:var(--down) }}
summary {{ cursor:pointer; margin-top:6px; color:var(--muted) }}
.legend {{ font-size:12px; color:var(--muted); margin:6px 0 10px }}
</style></head><body><main>
<h1>NFL Public Betting Splits</h1>
<div class="muted">DraftKings (all states) + Scores and Odds consensus &middot; updated {updated}</div>
<p class="legend">Small green/red numbers: change since first snapshot.
<b style="color:var(--fade)">fade?</b> = spread/total at {FADE_AT}%+ of bets (DK or consensus).
<b style="color:var(--sharp)">$ &gt; tix</b> = money % beats bet % by {SHARP_GAP}+.
<b style="color:var(--pub)">public</b> = {PUBLIC_HEAVY}%+ of tickets.</p>
<h3>Fade candidates ({FADE_AT}%+ of bets)</h3>
{fades}
<h3>Fade {FADE_AT}% record (tracked by this tool)</h3>
{build_record_html(plays)}
<h3>Money-heavy sides</h3>
{flags}
<div class="grid">{''.join(cards)}</div>
</main></body></html>"""


def report():
    try:
        n = grade_finished()
        if n:
            print(f"Graded {n} finished game(s)")
    except Exception as e:  # noqa: BLE001
        print(f"Grading skipped: {e}", file=sys.stderr)
    plays = fade_record()
    for src in ("dk", "sao"):
        sp = [p for p in plays if p["source"] == src]
        if sp:
            print(f"Fade {FADE_AT}%+ ({src}): {tally(sp)}")
    latest_ts, data = load_report_data()
    if not latest_ts:
        print("No data yet, run a scrape first.")
        return
    HTML_PATH.write_text(build_html(latest_ts, data, plays), encoding="utf-8")
    print(f"Dashboard written: {HTML_PATH}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--report", action="store_true", help="grade + dashboard only")
    ap.add_argument("--dump", action="store_true", help="save raw HTML for debugging")
    ap.add_argument("--from-file", nargs="+", metavar=("DK_HTML", "SAO_HTML"),
                    help="parse saved pages instead of fetching (testing)")
    args = ap.parse_args()

    if not args.report:
        if args.from_file:
            games = parse_dk(Path(args.from_file[0]).read_text())
            sao_html = Path(args.from_file[1]).read_text() if len(args.from_file) > 1 else ""
        else:
            games = scrape_dk(args.dump)
            try:
                sao_html = fetch(SAO_URL)
                if args.dump:
                    (HERE / "dump_sao.html").write_text(sao_html, encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                print(f"Scores and Odds fetch failed: {e}", file=sys.stderr)
                sao_html = ""
        if not games:
            print("No DK games parsed. DK may have changed the page layout "
                  "(rerun with --dump and send the files).", file=sys.stderr)
            sys.exit(1)
        matched = merge_sao(games, parse_sao(sao_html)) if sao_html else 0
        if sao_html and not matched:
            print("Scores and Odds: no games matched (layout may differ, try --dump).",
                  file=sys.stderr)
        ts = save(games)
        print(f"{ts}: saved {len(games)} games "
              f"({matched} with consensus data)")
    report()


if __name__ == "__main__":
    main()
