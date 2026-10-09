#!/bin/bash
# Commit and push the given paths from a GitHub Actions job, safe when other jobs push at the same time:
# the job's outputs are copied aside, laid on top of the newest main, then committed.
# Usage: bash commit.sh "message" path [path ...]
msg="$1"; shift
git config user.name "line-room-bot"
git config user.email "line-room-bot@users.noreply.github.com"
tmp=$(mktemp -d)
for p in "$@"; do [ -e "$p" ] && cp -r --parents "$p" "$tmp/"; done
for i in 1 2 3 4; do
  git fetch -q origin main && git reset -q --hard origin/main
  cp -r "$tmp/." .
  r=$(git add -f "$@" 2>&1) || { echo "::error::git add: $r"; exit 1; }
  if git diff --cached --quiet; then echo "::notice::No changes to save"; exit 0; fi
  r=$(git commit -qm "$msg" 2>&1) || { echo "::error::git commit: $r"; exit 1; }
  if r=$(git push -q origin HEAD:main 2>&1); then echo "::notice::Saved: $msg"; exit 0; fi
  sleep $((i * 4))
done
echo "::error::git push: $(echo "$r" | tr '\n' ' ')"; exit 1
