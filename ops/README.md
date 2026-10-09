# Line Room operations

Data collection runs on GitHub Actions (this repo). Claude scheduled tasks only publish the data
to the site and talk to Jake. Each task starts in a fresh session, so every procedure begins by
getting the repo:

    git clone --depth 1 https://github.com/VDevelopments-W/line-room /tmp/lr || git -C /tmp/lr pull -q
    cd /tmp/lr

Site: https://claude.ai/artifact/ADCCQBgq9bnZpS4WseTm2z (Artifact tool for files, ArtifactData tool for the database).
Unit: $20. Messages to Jake: short, mobile-friendly, no em dashes.

| File | When |
|---|---|
| daily.md | every morning (publish data files + database), Tuesday adds the weekly recap |
| weekly_setup.md | Wednesday: schedule this week's pre-kickoff checks |
| pregame.md | 15-20 minutes before each kickoff window |
