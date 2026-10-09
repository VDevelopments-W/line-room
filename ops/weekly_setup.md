# Weekly setup (Wednesday 9 AM Pacific)

1. Get the repo. From site/data/trends.json take `next_week` and `week_games` (gameday + gametime are US Eastern).
2. Group games into kickoff windows (games starting within 30 minutes of each other).
3. For each window create a one-shot scheduled task (create_trigger, run_once_at = 20 minutes before the
   window's first kickoff, in UTC; initiation "human_schedule"; notifications push on) named
   "Line Room check: <weekday> <time PT>" with prompt:
     "Line Room pre-kickoff check. Follow ops/pregame.md in https://github.com/VDevelopments-W/line-room
      for these games: <game_ids, comma separated>."
4. Message Jake one line: the windows scheduled and any game already showing 80%+ public at either source
   (from site/data/splits.json), so he can plan.
