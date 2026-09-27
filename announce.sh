#!/usr/bin/env bash
#
# Post an announcement to Discord from this box, without opening Discord.
#
#   ./announce.sh "Jellyfin down ~20 min for system updates"
#   ./announce.sh --title "Disk upgrade" "Moving the library to the new pool."
#   ./announce.sh --maintenance 20m "System updates"
#   ./announce.sh --done
#   ./announce.sh --done "Back up, all libraries rescanned."
#   ./announce.sh --health
#
# Reads ANNOUNCE_TOKEN / ANNOUNCE_PORT from .env beside this script. The token
# is passed to curl via a header file on stdin so it never appears in the
# process list, where any user on the box could read it from `ps`.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

usage() {
    sed -n '3,12p' "$0" | sed 's/^# \?//'
    exit "${1:-1}"
}

[[ $# -eq 0 ]] && usage

if [[ ! -f .env ]]; then
    echo "error: no .env beside this script." >&2
    exit 1
fi

# Only the two keys we need, so a malformed line elsewhere can't break us.
TOKEN="$(sed -n 's/^ANNOUNCE_TOKEN=//p' .env | tail -1)"
PORT="$(sed -n 's/^ANNOUNCE_PORT=//p' .env | tail -1)"
PORT="${PORT:-8765}"

if [[ -z "$TOKEN" ]]; then
    echo "error: ANNOUNCE_TOKEN is not set in .env, so the endpoint is disabled." >&2
    echo "Generate one with:  openssl rand -hex 32" >&2
    exit 1
fi

BASE="http://127.0.0.1:${PORT}"

# jq handles quoting and newlines correctly; fall back to a python one-liner.
json_string() {
    if command -v jq >/dev/null 2>&1; then
        jq -Rn --arg s "$1" '$s'
    else
        python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"
    fi
}

post() {
    local path="$1" body="$2" response http_code
    response="$(
        printf 'header "Authorization: Bearer %s"\n' "$TOKEN" |
            curl -sS --max-time 20 -K - \
                -w '\n%{http_code}' \
                -H 'Content-Type: application/json' \
                -X POST --data "$body" \
                "${BASE}${path}"
    )"
    http_code="$(tail -n1 <<<"$response")"
    body="$(sed '$d' <<<"$response")"

    if [[ "$http_code" == "200" ]]; then
        echo "posted."
        return 0
    fi
    echo "error: HTTP ${http_code}" >&2
    echo "$body" >&2
    return 1
}

TITLE=""
case "${1:-}" in
    --health)
        curl -sS --max-time 10 "${BASE}/health"; echo
        exit 0
        ;;
    --done)
        shift
        note="${1:-}"
        if [[ -n "$note" ]]; then
            post /maintenance/done "{\"note\": $(json_string "$note")}"
        else
            post /maintenance/done '{}'
        fi
        exit $?
        ;;
    --maintenance)
        shift
        [[ $# -ge 2 ]] || { echo "usage: $0 --maintenance <duration> <reason>" >&2; exit 1; }
        duration="$1"; shift
        post /maintenance/start \
            "{\"duration\": $(json_string "$duration"), \"reason\": $(json_string "$*")}"
        exit $?
        ;;
    --title)
        shift
        [[ $# -ge 2 ]] || { echo "usage: $0 --title <title> <message>" >&2; exit 1; }
        TITLE="$1"; shift
        ;;
    -h|--help)
        usage 0
        ;;
    -*)
        echo "error: unknown option $1" >&2
        usage
        ;;
esac

[[ $# -ge 1 ]] || usage

if [[ -n "$TITLE" ]]; then
    post /announce "{\"message\": $(json_string "$*"), \"title\": $(json_string "$TITLE")}"
else
    post /announce "{\"message\": $(json_string "$*")}"
fi
