#!/usr/bin/env bash
#
# add_tmdb_key.sh — store a TMDb API key in .env for jellyfin_imdb_rename.py
#
# Get a free key at: https://www.themoviedb.org/settings/api
# (Settings -> API -> "API Key (v3 auth)" — the 32-character hex one,
#  NOT the long "API Read Access Token".)
#
# Usage:
#   ./add_tmdb_key.sh              # prompts, input hidden
#   ./add_tmdb_key.sh <key>        # non-interactive

set -euo pipefail

ENV_FILE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env"
VAR="TMDB_API_KEY"

key="${1:-}"
if [ -z "$key" ]; then
    printf 'Paste your TMDb API key (v3 auth), input hidden: ' >&2
    read -rs key
    printf '\n' >&2
fi

key="$(printf '%s' "$key" | tr -d '[:space:]')"

if [ -z "$key" ]; then
    echo "No key entered. Nothing written." >&2
    exit 1
fi

# TMDb v3 keys are 32 lowercase hex characters. Warn, don't block — if TMDb
# ever changes the format this script shouldn't be the thing standing in the way.
if ! printf '%s' "$key" | grep -qE '^[0-9a-fA-F]{32}$'; then
    echo "Warning: that doesn't look like a v3 API key (expected 32 hex chars)." >&2
    echo "         If you copied the 'API Read Access Token', grab the shorter" >&2
    echo "         'API Key (v3 auth)' value instead." >&2
    printf 'Write it anyway? [y/N] ' >&2
    read -r reply </dev/tty
    case "$reply" in
        [yY]*) ;;
        *) echo "Aborted." >&2; exit 1 ;;
    esac
fi

touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Make sure the file ends in a newline before appending, or we'd glue the new
# variable onto the end of the last line.
if [ -s "$ENV_FILE" ] && [ "$(tail -c1 "$ENV_FILE" | wc -l)" -eq 0 ]; then
    printf '\n' >> "$ENV_FILE"
fi

if grep -qE "^[[:space:]]*${VAR}=" "$ENV_FILE"; then
    cp "$ENV_FILE" "$ENV_FILE.bak"
    # Replace in place. Uses a control char as the delimiter so the key's
    # contents can never be mistaken for the separator.
    sed -i "s"$'\001'"^[[:space:]]*${VAR}=.*"$'\001'"${VAR}=${key}"$'\001' "$ENV_FILE"
    echo "Updated ${VAR} in ${ENV_FILE} (previous file saved as .env.bak)"
else
    {
        printf '\n# ---- TMDb --------------------------------------------------------------\n'
        printf '# Free key: https://www.themoviedb.org/settings/api (API Key, v3 auth).\n'
        printf '# Only used to look up movies that have no IMDb ID anywhere.\n'
        printf '%s=%s\n' "$VAR" "$key"
    } >> "$ENV_FILE"
    echo "Added ${VAR} to ${ENV_FILE}"
fi

echo "Verifying against the TMDb API..."
code="$(curl -s -o /dev/null -w '%{http_code}' -m 15 \
        "https://api.themoviedb.org/3/configuration?api_key=${key}" || echo 000)"

case "$code" in
    200) echo "  OK — TMDb accepted the key." ;;
    401) echo "  FAILED — TMDb rejected the key (401). Check you copied the v3 API key." >&2; exit 1 ;;
    000) echo "  Could not reach TMDb to verify. Key was still written." >&2 ;;
    *)   echo "  Unexpected response from TMDb (HTTP $code). Key was still written." >&2 ;;
esac
