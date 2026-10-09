"""
Quarterback ratings, walk-forward.

Each QB's rating is his efficiency (EPA per dropback, passes + sacks + QB
runs), weighted toward recent games and shrunk toward a backup-level prior
until he has a real sample. A rookie or a backup who has barely played sits
near the prior.

rating(qb, before_order) only uses games played before `before_order`
(season * 100 + week), so the backtest never peeks.
"""
import bisect
import glob
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
GAME_DECAY = 0.97    # weight per game back (half-life about 23 games)
K = 400              # dropbacks of prior weight (tuned 2006-2016)
PRIOR = -0.15        # EPA/play of an unknown or backup QB


class QBRatings:
    def __init__(self):
        frames = []
        cols = ["player_id", "player_display_name", "position", "season", "week", "team",
                "attempts", "sacks_suffered", "carries", "passing_epa", "rushing_epa"]
        for f in sorted(glob.glob(str(HERE / "data" / "players" / "w*.csv.gz"))):
            d = pd.read_csv(f, usecols=lambda c: c in cols, low_memory=False)
            frames.append(d[d.position == "QB"])
        q = pd.concat(frames, ignore_index=True)
        q["plays"] = q["attempts"].fillna(0) + q["sacks_suffered"].fillna(0) + q["carries"].fillna(0)
        q["epa"] = q["passing_epa"].fillna(0) + q["rushing_epa"].fillna(0)
        q = q[q.plays >= 5]
        q["order"] = q.season * 100 + q.week
        q = q.sort_values(["player_id", "order"])
        self.names = q.groupby("player_id")["player_display_name"].last().to_dict()
        self.idx = {}
        for pid, d in q.groupby("player_id", sort=False):
            se = sp = 0.0
            orders, S_e, S_p = [], [], []
            for o, e, p in zip(d.order.to_numpy(), d.epa.to_numpy(), d.plays.to_numpy()):
                se = se * GAME_DECAY + e
                sp = sp * GAME_DECAY + p
                orders.append(int(o)); S_e.append(se); S_p.append(sp)
            self.idx[pid] = (orders, S_e, S_p)

    def rating(self, pid, before_order):
        rec = self.idx.get(pid) if isinstance(pid, str) else None
        if not rec:
            return PRIOR, 0.0
        orders, S_e, S_p = rec
        i = bisect.bisect_left(orders, before_order) - 1
        if i < 0:
            return PRIOR, 0.0
        return (S_e[i] + K * PRIOR) / (S_p[i] + K), S_p[i]

    def name(self, pid):
        return self.names.get(pid, "")
