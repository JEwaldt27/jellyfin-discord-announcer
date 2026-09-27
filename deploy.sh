#!/usr/bin/env bash
#
# Deploy the announcer on this box: pull from GitHub, rebuild, restart.
#
# Run it from anywhere - it works in its own directory:
#   ssh jewaldt@192.168.12.234 './jellyfin-discord-bot/deploy.sh'
#
# Changes are made on the workstation and pushed to GitHub; this box only ever
# pulls. The script refuses to run if the working tree here has local edits,
# because a box quietly diverging from the repo is the exact failure this
# setup exists to prevent.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "error: this checkout has uncommitted changes, refusing to deploy." >&2
    echo >&2
    git status --short >&2
    echo >&2
    echo "Make changes on your workstation and push them. If the edits here are" >&2
    echo "wanted, commit and push them from this box first; if not, discard them." >&2
    exit 1
fi

echo "==> Pulling from $(git remote get-url origin)"
git pull --ff-only origin main

GIT_COMMIT="$(git rev-parse --short HEAD)"
BUILD_DATE="$(date -u +%Y-%m-%dT%H:%MZ)"
export GIT_COMMIT BUILD_DATE

echo
echo "==> Building ${GIT_COMMIT} (${BUILD_DATE})"
docker compose up -d --build

echo
echo "==> Container"
docker compose ps --format '{{.Name}}  {{.Status}}'

if command -v python3 >/dev/null 2>&1; then
    echo
    echo "==> /version should report: $(python3 version.py)"
fi

echo
echo "==> Recent logs"
docker compose logs --tail 20
