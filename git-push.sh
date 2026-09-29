#!/usr/bin/env bash
# Rituel git : pull -> add -> commit -> push
# Usage : ./git-push.sh "message du commit" [fichiers...]
#   - sans fichiers : stage tout
#   - ex. : ./git-push.sh "fix: durée musique 22s"
set -euo pipefail
cd "$(dirname "$0")"

if [ $# -lt 1 ]; then
    echo "Usage: $0 \"message du commit\" [fichiers...]" >&2
    echo "  sans fichiers -> git add -A" >&2
    exit 1
fi
MSG="$1"; shift

echo "==> git pull"
git pull --ff-only

if [ $# -gt 0 ]; then
    echo "==> git add $*"
    git add "$@"
else
    echo "==> git add -A"
    git add -A
fi

echo "==> fichiers stagés:"
git diff --cached --stat | tail -5

echo "==> git commit"
git commit -m "$MSG"

echo "==> git push"
git push
echo "OK ✅"