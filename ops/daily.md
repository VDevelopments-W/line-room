# Daily (6 AM Pacific, after the GitHub "Daily rebuild" at 5 AM)

1. Get the repo (README). Check the rebuild ran today: `git log -1 --format=%cd --grep "Daily rebuild"`.
   If it is older than 6 hours, tell Jake in one line ("Daily rebuild didn't run, site data is from <date>")
   and still continue.
2. Follow publish.md: database, then data files.
3. Tuesday only: send Jake the weekly recap (SendUserMessage + PushNotification):
   - From site/data/signals.json: last week's record for each signal group with games (w-l, CLV),
     and the season total for the three fade groups and the two prop groups. One line each.
   - Bet log: ArtifactData `list` collection `bets`; last week's bets with result and CLV
     (CLV = our line minus the closing line from site/data/trends.json season_games, sign as in the site).
   - One sentence: which signal is pulling its weight and which isn't, only if the sample is 10+.
   Other days: no message unless something failed.
