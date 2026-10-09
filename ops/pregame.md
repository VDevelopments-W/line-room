# Pre-kickoff check

Input: the game ids named in the task prompt.

1. Get the repo (README). Splits are collected every 15 minutes by GitHub; use site/data/splits.json and
   data/splits_history/<game_id>.json. If the newest snapshot for a game is older than 45 minutes, WebFetch
   https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/?tb_eg=88808&tb_edate=today and
   https://www.scoresandodds.com/nfl/consensus-picks for that game instead.
2. Fade rules (spreads and totals only), on the side with the most bets:
   - 80%+ of bets at BOTH DraftKings and Scores and Odds: 1u ($20)
   - 80%+ at one source only: 0.5u ($10)
   - DraftKings money % above its bets % on that side (big money with the public too): drop to 0.5u
   - Below 80% at both: skip. 70-79% = "watch", mention but don't bet.
   Say which source triggered each play.
3. Line movement: compare the first and last snapshot in data/splits_history. If 60%+ of bets are on a side
   and the line moved 0.5+ in that side's favor, flag "line moving against the public" (supports the fade).
4. Injuries: WebSearch the inactives/injury news for each team in a play (inactives post ~90 min before
   kickoff). Call out a starting QB change or key starter out, and whether it changes the play.
5. Props: from site/data/kalshi.json and site/data/propmodel.json, list up to 3 props in these games where
   the model's chance beats Kalshi's ask by 5+ points, ask-bid spread <= 6 cents and volume >= 50.
   (Model chance = prop_model.prob(model, player, stat, line, opp, home, implied) in python.)
   Also WebSearch "dknetwork player prop bet splits <team> <team>" and list any prop with 80%+ of bets on one side.
6. Send Jake (SendUserMessage + PushNotification): each play with line, % bets / % money at each source,
   size, injury notes; watch-tier sides; the prop list. Mobile-short, no em dashes.
7. Follow publish.md, database part only.
