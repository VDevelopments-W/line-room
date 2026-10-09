#!/bin/bash
# Commit and push the given paths from a GitHub Actions job. Reports problems as annotations.
# Usage: bash commit.sh "message" path [path ...]
msg="$1"; shift
git config user.name "line-room-bot"
git config user.email "line-room-bot@users.noreply.github.com"
r=$(git add -f "$@" 2>&1) || { echo "::error::git add: $r"; exit 1; }
if git diff --cached --quiet; then echo "::notice::No changes to save"; exit 0; fi
r=$(git commit -qm "$msg" 2>&1) || { echo "::error::git commit: $r"; exit 1; }
for i in 1 2 3; do
  git pull --rebase -q origin main >/dev/null 2>&1
  if r=$(git push 2>&1); then echo "::notice::Saved: $msg"; exit 0; fi
  sleep $((i * 5))
done
echo "::error::git push: $(echo "$r" | tr '\n' ' ')"; exit 1
