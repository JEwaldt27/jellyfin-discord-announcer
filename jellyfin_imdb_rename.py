#!/usr/bin/env python3
"""
jellyfin_imdb_rename.py

1. Scans a Jellyfin server for all movies.
2. For movies with no IMDb ID, looks one up via TMDb (title + year search).
3. Reports any movie it could not match.
4. Renames the media file (and sidecar subtitles/nfo) to:
       Title (Year) [imdbid-tt0469903].mkv

Dry-run by default. Nothing is renamed until you pass --apply.

No third-party dependencies — standard library only, so it runs on a Windows
PC over SMB or directly on the TrueNAS box over SSH with no pip install.
"""

import argparse
import csv
import difflib
import json
import os
import re
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath, PurePosixPath


# ─────────────────────────────────────────────────────────────────────────────
#  API KEYS  —  read from the environment / .env
# ─────────────────────────────────────────────────────────────────────────────

def load_dotenv(path=None):
    """
    Minimal .env reader, so this stays dependency-free.

    Looks next to the script, not in the current directory, so the script works
    from anywhere. A variable already set in the real environment always wins:
        TMDB_API_KEY=xxx python3 jellyfin_imdb_rename.py
    """
    if path is None:
        path = Path(__file__).resolve().parent / ".env"
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv()


def get_key(name, required=True, hint=""):
    """Read an API key from the environment, or exit explaining how to set it."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if not required:
        return None
    env_file = Path(__file__).resolve().parent / ".env"
    sys.exit(
        f"\nMissing {name}.\n"
        f"  Add it to {env_file} as:\n"
        f"    {name}=your-key-here\n"
        + (f"  {hint}\n" if hint else "")
    )


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION  —  edit this block
# ─────────────────────────────────────────────────────────────────────────────

# Comes from .env. The literal is only a placeholder for running this script
# somewhere with no .env at all — set JELLYFIN_URL properly. No trailing slash.
JELLYFIN_URL = (os.environ.get("JELLYFIN_URL", "").strip()
                or "http://192.168.1.100:8096")

# API keys come from .env (see get_key below), not from this file.
#   JELLYFIN_API_KEY  Jellyfin Dashboard -> Advanced -> API Keys
#   TMDB_API_KEY      optional, free: https://www.themoviedb.org/settings/api
#                     only needed for movies with no IMDb ID anywhere.

# Translate paths from how Jellyfin sees them -> how THIS machine sees them.
#
#   MOVIES_JELLYFIN_PATH  the prefix Jellyfin reports. Find it by opening any
#                         movie in Jellyfin and reading its file path — if that
#                         is /movies/Heat (1995).mkv, this is /movies.
#   MOVIES_LOCAL_PATH     where those same files are mounted on the machine
#                         running this script.
#
# Both live in .env so there is one place to change them, and docker-compose
# reads MOVIES_LOCAL_PATH from the same file to mount the share into the
# container at a matching path.
PATH_MAP = [
    (os.environ.get("MOVIES_JELLYFIN_PATH", "").strip() or "/movies",
     os.environ.get("MOVIES_LOCAL_PATH", "").strip() or "/mnt/movies"),
]

# Your library is FLAT — movie files sit directly in the share root with no
# per-movie folders. Leave this False. Turning it on would try to rename the
# share root itself; there is a hard guard below, but do not tempt it.
RENAME_PARENT_FOLDER = False

# Also rename sidecar files that share the media file's stem
# (subtitles, .nfo, artwork). Strongly recommended.
RENAME_SIDECARS = True

# Sidecar extensions to move along with the video.
SIDECAR_EXTS = {
    ".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt", ".sup",
    ".nfo", ".jpg", ".jpeg", ".png", ".tbn", ".txt",
}

# Trigger Jellyfin's "Scan Media Library" task before doing anything else and
# wait for it to finish, so this script reads fresh metadata rather than
# whatever Jellyfin happened to have cached. Override for one run with
# --no-scan. A timeout here is not fatal — the run continues on stale data.
SCAN_BEFORE_RUN = True
SCAN_TIMEOUT = 1800       # seconds to wait for the scan before giving up
SCAN_POLL_INTERVAL = 3    # seconds between progress checks

# Write imdb_report.csv and imdb_rename.log to the working directory when the
# run finishes. False keeps the folder clean — everything is still printed to
# the terminal, so pipe it if you want a copy:
#     python3 jellyfin_imdb_rename.py | tee run.log
WRITE_REPORT_FILES = False

# Minimum title similarity (0.0–1.0) required to accept a TMDb match
# when the release year does not corroborate it.
FUZZY_THRESHOLD = 0.85

# Seconds between TMDb calls. TMDb allows ~50/sec; this is deliberately polite.
TMDB_DELAY = 0.10

# Windows-illegal characters get stripped regardless of platform, so the
# resulting names stay portable across SMB shares.
ILLEGAL_CHARS = r'<>:"/\|?*'


# ─────────────────────────────────────────────────────────────────────────────
#  JELLYFIN
# ─────────────────────────────────────────────────────────────────────────────

class HttpError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:200]}")


def http_json(url, params=None, headers=None, method="GET", timeout=30):
    """Minimal stdlib JSON fetch. Returns (parsed_json, status)."""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        url = f"{url}?{urllib.parse.urlencode(clean)}"
    req = urllib.request.Request(url, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return (json.loads(raw) if raw.strip() else {}), resp.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise HttpError(e.code, body) from None
    except urllib.error.URLError as e:
        raise HttpError(0, str(e.reason)) from None


class Jellyfin:
    def __init__(self, base_url, api_key):
        self.base = base_url.rstrip("/")
        self.headers = {
            "Authorization": (
                f'MediaBrowser Token="{api_key}", '
                'Client="imdb-rename-script", '
                'Device="script", '
                'DeviceId="imdb-rename-script-01", '
                'Version="1.0.0"'
            ),
            "Accept": "application/json",
        }

    def _get(self, path, **params):
        data, _ = http_json(f"{self.base}{path}", params, self.headers)
        return data

    def check(self):
        info = self._get("/System/Info")
        return info.get("ServerName", "?"), info.get("Version", "?")

    def movies(self, page_size=500):
        """Yield every movie as a dict. Paginated — safe on huge libraries."""
        start = 0
        while True:
            data = self._get(
                "/Items",
                recursive="true",
                includeItemTypes="Movie",
                fields="ProviderIds,Path,ProductionYear,MediaSources",
                enableImages="false",
                startIndex=start,
                limit=page_size,
                sortBy="SortName",
                sortOrder="Ascending",
            )
            items = data.get("Items", [])
            if not items:
                break
            for it in items:
                yield it
            start += len(items)
            if start >= data.get("TotalRecordCount", 0):
                break

    def refresh_library(self):
        http_json(f"{self.base}/Library/Refresh", headers=self.headers, method="POST")

    # ── Scheduled tasks ──────────────────────────────────────────────────
    #
    # /Library/Refresh above fires the scan but tells you nothing about it.
    # To *wait* for a scan we drive the same work through the scheduled-task
    # API instead, which exposes a State we can poll.

    LIBRARY_SCAN_KEY = "RefreshLibrary"

    def find_task(self, key):
        """Return the scheduled task with the given Key, or None."""
        tasks = self._get("/ScheduledTasks")
        for t in tasks or []:
            if t.get("Key") == key:
                return t
        return None

    def get_task(self, task_id):
        return self._get(f"/ScheduledTasks/{task_id}") or {}

    def start_task(self, task_id):
        """Start a scheduled task. Returns 204 with an empty body."""
        http_json(f"{self.base}/ScheduledTasks/Running/{task_id}",
                  headers=self.headers, method="POST")


# ─────────────────────────────────────────────────────────────────────────────
#  TMDB
# ─────────────────────────────────────────────────────────────────────────────

class TMDb:
    BASE = "https://api.themoviedb.org/3"

    def __init__(self, api_key):
        self.key = api_key
        self._cache = {}

    def _get(self, path, **params):
        if not self.key:
            raise RuntimeError(
                "no TMDB_API_KEY configured — cannot search TMDb"
            )
        params["api_key"] = self.key
        last = None
        for attempt in range(5):
            try:
                data, _ = http_json(f"{self.BASE}{path}", params)
                time.sleep(TMDB_DELAY)
                return data
            except HttpError as e:
                last = e
                if e.status == 401:
                    raise RuntimeError("TMDb rejected the API key (401).") from None
                # 429 = rate limited. 0 = dropped/reset connection (WinError
                # 10054 and friends), which TMDb does intermittently over TLS.
                # 5xx = transient server error. All worth retrying.
                if e.status in (0, 429) or e.status >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"TMDb failed after 5 attempts on {path}: {last}")

    def find_ids(self, title, year):
        """
        Return (imdb_id, tmdb_id, note). Either ID may be None.
        A TMDb hit with no IMDb ID still gives us a usable tmdb_id.
        """
        attempts = []
        if year:
            attempts.append({"query": title, "year": year})
            attempts.append({"query": title, "primary_release_year": year - 1})
            attempts.append({"query": title, "primary_release_year": year + 1})
        attempts.append({"query": title})

        seen_any = False
        for params in attempts:
            data = self._get("/search/movie", **params)
            results = data.get("results", [])
            if not results:
                continue
            seen_any = True

            best = self._best_match(title, year, results)
            if best is None:
                continue

            cand, score, note = best
            tmdb_id = str(cand["id"])
            imdb = self._imdb_for(cand["id"])
            detail = f"{note} (tmdb:{tmdb_id}, match {score:.0%})"
            if imdb:
                return imdb, tmdb_id, detail
            return None, tmdb_id, detail + ", no IMDb ID on TMDb"

        return None, None, ("no TMDb results" if not seen_any else "no confident match")

    def _best_match(self, title, year, results):
        norm_target = normalize_for_match(title)
        scored = []
        for r in results:
            for key in ("title", "original_title"):
                cand_title = r.get(key)
                if not cand_title:
                    continue
                norm_cand = normalize_for_match(cand_title)
                score = difflib.SequenceMatcher(None, norm_target, norm_cand).ratio()

                # Franchise prefixes and subtitles wreck raw string similarity:
                # "Insanity: Dig Deeper & Fit Test" vs "Dig Deeper & Fit Test"
                # scores ~0.72. If one title's words are a subset of the
                # other's, treat it as a strong match instead.
                score = max(score, containment_score(norm_target, norm_cand))

                rel = r.get("release_date") or ""
                cand_year = int(rel[:4]) if rel[:4].isdigit() else None

                # tier 0 beats tier 1 beats tier 2, regardless of title score.
                # Without this, a same-titled remake can outrank the correct year.
                if year and cand_year == year and score >= 0.60:
                    scored.append((0, r, score, "year+title"))
                elif year and cand_year and abs(cand_year - year) <= 1 and score >= 0.75:
                    scored.append((1, r, score, "year±1"))
                elif score >= FUZZY_THRESHOLD and (norm_cand == norm_target
                                                   or containment_score(norm_target, norm_cand) > 0):
                    # Title-only matches have no year to corroborate them, so
                    # a high fuzzy ratio alone is not enough: "Alien" vs
                    # "Aliens" scores 91%. Demand an exact normalized match or
                    # a whole-word containment match instead.
                    scored.append((2, r, score, "title only"))

        if not scored:
            return None
        scored.sort(key=lambda t: (t[0], -t[2]))
        tier, cand, score, note = scored[0]
        return cand, score, note

    def _imdb_for(self, tmdb_id):
        if tmdb_id in self._cache:
            return self._cache[tmdb_id]
        data = self._get(f"/movie/{tmdb_id}/external_ids")
        imdb = data.get("imdb_id") or None
        if imdb and not re.fullmatch(r"tt\d{7,9}", imdb):
            imdb = None
        self._cache[tmdb_id] = imdb
        return imdb


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def normalize_for_match(s):
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\b(the|a|an)\b", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def containment_score(a, b):
    """
    Score for one normalized title being contained in the other.

    Word-set based, not substring based, so "Up" does not match "Grown Ups".
    Requires the shorter title to be at least 2 words, which is what keeps
    generic one-word titles from matching everything.
    """
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(shorter) < 2 or not shorter.issubset(longer):
        return 0.0
    # More overlap = more confidence. Never quite reaches 1.0, so an exact
    # match still outranks a containment match.
    return 0.85 + 0.14 * (len(shorter) / len(longer))


def sanitize(name):
    """Strip filesystem-illegal characters and trailing dots/spaces."""
    for ch in ILLEGAL_CHARS:
        name = name.replace(ch, "")
    name = re.sub(r"\s+", " ", name).strip()
    return name.rstrip(". ")


def translate_path(jf_path):
    """Map a Jellyfin-reported path onto this machine's filesystem."""
    if not jf_path:
        return None
    for jf_prefix, local_prefix in PATH_MAP:
        if jf_path.lower().startswith(jf_prefix.lower()):
            remainder = jf_path[len(jf_prefix):].lstrip("/\\")
            if "\\" in local_prefix or re.match(r"^[A-Za-z]:", local_prefix):
                return str(PureWindowsPath(local_prefix) / PureWindowsPath(remainder))
            return str(PurePosixPath(local_prefix) / PurePosixPath(remainder.replace("\\", "/")))
    return jf_path


def target_stem(title, year, tag):
    """
    `tag` is a full provider tag such as 'imdbid-tt0469903' or 'tmdbid-393112'.
    """
    year_part = f" ({year})" if year else ""
    return sanitize(f"{title}{year_part} [{tag}]")


def existing_imdb(item):
    pid = item.get("ProviderIds") or {}
    for key, val in pid.items():
        if key.lower() == "imdb" and val:
            val = str(val).strip()
            if re.fullmatch(r"tt\d{7,9}", val):
                return val
    return None


def existing_tmdb(item):
    """Jellyfin usually knows the TMDb ID even when it has no IMDb ID."""
    pid = item.get("ProviderIds") or {}
    for key, val in pid.items():
        if key.lower() in ("tmdb", "themoviedb") and val:
            val = str(val).strip()
            if re.fullmatch(r"\d+", val):
                return val
    return None


def id_in_filename(path):
    """Return an existing provider tag already present in the filename."""
    name = Path(path).name
    m = re.search(r"\[(imdbid-tt\d{7,9})\]", name, re.IGNORECASE)
    if m:
        return m.group(1).lower().replace("imdbid-TT", "imdbid-tt")
    m = re.search(r"\[(tmdbid-\d+)\]", name, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  RENAMING
# ─────────────────────────────────────────────────────────────────────────────

_DIR_CACHE = {}


def dir_files(folder):
    """
    Names of files in `folder`, scanned once and cached.

    Critical for flat libraries: without the cache this function runs once per
    movie against the same directory, which over SMB means hundreds of full
    directory listings and hundreds of thousands of round trips.
    os.scandir() also reuses the directory entry's type info, so is_file()
    costs no extra network call.
    """
    key = str(folder).lower()
    if key not in _DIR_CACHE:
        try:
            with os.scandir(folder) as it:
                _DIR_CACHE[key] = [e.name for e in it if e.is_file()]
        except OSError:
            _DIR_CACHE[key] = []
    return _DIR_CACHE[key]


def cache_rename(folder, old_name, new_name):
    """Keep the cached listing accurate after a rename."""
    key = str(folder).lower()
    names = _DIR_CACHE.get(key)
    if names is None:
        return
    if old_name in names:
        names[names.index(old_name)] = new_name
    elif new_name not in names:
        names.append(new_name)


def plan_renames(local_path, new_stem):
    """
    Return a list of (src, dst) pairs for the media file plus any sidecars.
    Returns [] if nothing needs to change.
    """
    src = Path(local_path)
    parent = src.parent
    names = dir_files(parent)

    if src.name not in names and not src.exists():
        raise FileNotFoundError(local_path)

    old_stem = src.stem
    pairs = []

    dst = src.with_name(new_stem + src.suffix)
    if dst != src:
        pairs.append((src, dst))

    if RENAME_SIDECARS:
        for name in names:
            if name == src.name or not name.startswith(old_stem):
                continue
            # Matches "Movie.srt", "Movie.en.srt", "Movie.en.forced.srt"
            tail = name[len(old_stem):]
            if not tail.startswith("."):
                continue
            if Path(name).suffix.lower() not in SIDECAR_EXTS:
                continue
            sib = parent / name
            sib_dst = sib.with_name(new_stem + tail)
            if sib_dst != sib:
                pairs.append((sib, sib_dst))

    return pairs


def execute_renames(pairs, apply_changes, log):
    existing = None
    for src, dst in pairs:
        if existing is None:
            existing = set(n.lower() for n in dir_files(src.parent))
        if dst.name.lower() in existing and dst.name.lower() != src.name.lower():
            log(f"    SKIP (target exists): {dst.name}")
            continue
        log(f"    {src.name}")
        log(f"      -> {dst.name}")
        if apply_changes:
            try:
                src.rename(dst)
                cache_rename(src.parent, src.name, dst.name)
                existing.discard(src.name.lower())
                existing.add(dst.name.lower())
            except OSError as e:
                log(f"      !! FAILED: {e}")


def is_protected_root(folder):
    """
    True if `folder` is a library/share root that must never be renamed.
    Covers: any PATH_MAP destination, a UNC share root (\\\\host\\share),
    and a filesystem/drive root.
    """
    f = Path(folder)
    resolved = str(f).rstrip("\\/").lower()

    for _, local_prefix in PATH_MAP:
        if resolved == str(Path(local_prefix)).rstrip("\\/").lower():
            return True

    # UNC share root: \\host\share has exactly 4 leading-empty parts when split
    s = str(f)
    if s.startswith("\\\\"):
        parts = [p for p in s.strip("\\").split("\\") if p]
        if len(parts) <= 2:          # \\host  or  \\host\share
            return True

    if f.parent == f:                # C:\  or  /
        return True

    return False


def maybe_rename_folder(local_path, title, year, apply_changes, log):
    """Rename the movie's own folder to 'Title (Year)'. Never touches a root."""
    parent = Path(local_path).parent
    desired = sanitize(f"{title} ({year})" if year else title)

    if is_protected_root(parent):
        log(f"    REFUSING folder rename — '{parent}' is a share/library root.")
        log(f"    (Your library is flat; set RENAME_PARENT_FOLDER = False.)")
        return None

    if parent.name == desired:
        return None

    siblings = [p for p in parent.iterdir() if p.is_dir()]
    if siblings:
        log(f"    SKIP folder rename (has subdirectories): {parent.name}")
        return None

    new_parent = parent.with_name(desired)
    if new_parent.exists():
        log(f"    SKIP folder rename (target exists): {desired}")
        return None

    log(f"    [dir] {parent.name}  ->  {desired}")
    if apply_changes:
        try:
            parent.rename(new_parent)
            return new_parent
        except OSError as e:
            log(f"      !! FOLDER RENAME FAILED: {e}")
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────────────

def scan_library_and_wait(jf, log, timeout=SCAN_TIMEOUT, poll=SCAN_POLL_INTERVAL):
    """
    Kick off Jellyfin's library scan and block until it finishes.

    Returns True if the scan completed, False if it timed out, could not be
    found, or errored. A False return is deliberately not fatal: stale
    metadata is a much smaller problem than refusing to run at all, so the
    caller logs it and carries on.
    """
    try:
        task = jf.find_task(Jellyfin.LIBRARY_SCAN_KEY)
    except Exception as e:
        log(f"      Could not query scheduled tasks ({e}); skipping the scan.")
        return False

    if not task:
        log(f"      No '{Jellyfin.LIBRARY_SCAN_KEY}' task on this server; skipping the scan.")
        return False

    task_id = task["Id"]
    task_name = task.get("Name", "Scan Media Library")

    # Jellyfin runs this task on its own schedule too, and other clients can
    # start it. Starting a second one is refused/queued, so if one is already
    # in flight just attach to it.
    if task.get("State") == "Running":
        log(f"      '{task_name}' is already running — waiting for it.")
    else:
        try:
            jf.start_task(task_id)
        except Exception as e:
            log(f"      Could not start '{task_name}' ({e}); continuing without a scan.")
            return False
        log(f"      Started '{task_name}'.")

    deadline = time.time() + timeout

    # Phase A: wait for the task to actually leave Idle.
    #
    # Jellyfin does not flip State to Running synchronously with the POST, so
    # polling immediately can catch it still Idle and conclude the scan is
    # already done. Give it a grace window to start. If it never starts, the
    # scan was short enough to finish inside the window — which is a success,
    # not a failure, so fall through either way.
    grace_deadline = min(time.time() + 15, deadline)
    started = False
    while time.time() < grace_deadline:
        try:
            if jf.get_task(task_id).get("State") == "Running":
                started = True
                break
        except Exception:
            pass          # transient; the main loop below will surface it
        time.sleep(1)

    if not started:
        log("      Scan finished immediately (nothing to update).")
        return True

    # Phase B: poll until it returns to Idle.
    last_line = ""
    while time.time() < deadline:
        time.sleep(poll)
        try:
            t = jf.get_task(task_id)
        except Exception as e:
            # A scan can briefly make the server unresponsive. Keep waiting;
            # only the deadline ends this loop.
            log(f"      (lost contact with Jellyfin: {e} — still waiting)")
            continue

        state = t.get("State")
        if state != "Running":
            result = (t.get("LastExecutionResult") or {}).get("Status", "?")
            elapsed = int(time.time() - (deadline - timeout))
            if last_line:
                print(" " * len(last_line), end="\r")
            log(f"      Scan finished in {elapsed}s (status: {result}).")
            return result in ("Completed", "?")

        pct = t.get("CurrentProgressPercentage")
        if pct is not None:
            last_line = f"      scanning... {pct:5.1f}%".ljust(40)
            print(last_line, end="\r", flush=True)

    if last_line:
        print(" " * len(last_line), end="\r")
    log(f"      Scan did not finish within {timeout}s — continuing with "
        f"the metadata Jellyfin has now.")
    return False


def main():
    ap = argparse.ArgumentParser(
        description="Match Jellyfin movies to IMDb IDs and rename the files."
    )
    ap.add_argument("--apply", action="store_true",
                    help="Actually rename files. Without this, dry run only.")
    ap.add_argument("--no-rename", action="store_true",
                    help="Only do the IMDb matching and report; skip renaming.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only process the first N movies (for testing).")
    ap.add_argument("--refresh", action="store_true",
                    help="Trigger a Jellyfin library scan when finished.")
    ap.add_argument("--no-scan", action="store_true",
                    help="Skip the library scan this run normally does first "
                         f"(SCAN_BEFORE_RUN is currently {SCAN_BEFORE_RUN}).")
    ap.add_argument("--report", default="imdb_report.csv",
                    help="Where to write the CSV report. Only used when "
                         "WRITE_REPORT_FILES is True (currently "
                         f"{WRITE_REPORT_FILES}).")
    args = ap.parse_args()

    apply_changes = args.apply

    log_lines = []
    def log(msg=""):
        print(msg)
        log_lines.append(msg)

    if not PATH_MAP:
        log("Warning: PATH_MAP is empty. Paths will be used exactly as Jellyfin reports them.")

    print("Reading API keys from the environment/.env...")
    jellyfin_key = get_key(
        "JELLYFIN_API_KEY",
        hint="Jellyfin Dashboard -> Advanced -> API Keys -> +",
    )
    print(f"  JELLYFIN_API_KEY: {len(jellyfin_key)} chars")

    # Optional: only consulted for movies with no IMDb ID that also have no
    # usable ID in the filename or in Jellyfin's own metadata.
    tmdb_key = get_key("TMDB_API_KEY", required=False)
    if tmdb_key:
        print(f"  TMDB_API_KEY:     {len(tmdb_key)} chars")
    else:
        print("  TMDB_API_KEY:     not set — TMDb lookups will be skipped.")
        print("                    Run ./add_tmdb_key.sh to add one.")

    jf = Jellyfin(JELLYFIN_URL, jellyfin_key)
    tmdb = TMDb(tmdb_key)

    try:
        name, version = jf.check()
    except Exception as e:
        sys.exit(f"Cannot reach Jellyfin at {JELLYFIN_URL}: {e}")

    log(f"Connected to '{name}' (Jellyfin {version})")
    log(f"Mode: {'APPLY — files will be renamed' if apply_changes else 'DRY RUN — nothing will change'}")

    # ── Preflight: can we actually reach and write to the share? ─────────────
    for jf_prefix, local_prefix in PATH_MAP:
        root = Path(local_prefix)
        if not root.exists():
            sys.exit(
                f"\nCannot reach {local_prefix}\n"
                f"  Check the mount is up:  mount | grep {local_prefix}\n"
                f"  If it is an autofs/CIFS mount, touch it first: ls {local_prefix}\n"
                f"  Otherwise update PATH_MAP at the top of this script."
            )
        log(f"Share OK: {local_prefix}")
        if apply_changes:
            probe = root / ".imdb_rename_write_test.tmp"
            try:
                probe.write_text("x")
                probe.unlink()
                log("  Write permission: OK")
            except OSError as e:
                sys.exit(
                    f"\nNo write permission on {local_prefix} ({e})\n"
                    f"  Renaming will fail. Check the CIFS mount options (uid/gid)\n"
                    f"  and the SMB user's ACL on the TrueNAS dataset —\n"
                    f"  read-only access is not enough."
                )

    log("=" * 70)

    # ── Phase 1: refresh Jellyfin's view of the library ──────────────────────
    #
    # Deliberately after the share preflight: a broken mount should fail in
    # seconds, not after sitting through a full library scan.
    if args.no_scan or not SCAN_BEFORE_RUN:
        reason = "--no-scan" if args.no_scan else "SCAN_BEFORE_RUN is False"
        log(f"\n[1/5] Skipping library scan ({reason}).")
    else:
        log("\n[1/5] Scanning the Jellyfin library first...")
        scan_library_and_wait(jf, log)

    # ── Phase 2: collect movies ──────────────────────────────────────────────
    log("\n[2/5] Fetching movies from Jellyfin...")
    movies = list(jf.movies())
    if args.limit:
        movies = movies[:args.limit]
    log(f"      {len(movies)} movies found.")

    with_id = [m for m in movies if existing_imdb(m)]
    without_id = [m for m in movies if not existing_imdb(m)]
    log(f"      {len(with_id)} already have an IMDb ID.")
    log(f"      {len(without_id)} are missing one.")

    # ── Phase 3: resolve missing IDs ─────────────────────────────────────────
    log(f"\n[3/5] Resolving {len(without_id)} movies without an IMDb ID...")
    resolved = {}      # jellyfin item id -> provider tag ("imdbid-tt.." / "tmdbid-..")
    via_tmdb_id = 0
    unmatched = []     # list of (title, year, path, reason)

    for i, m in enumerate(without_id, 1):
        title = m.get("Name", "").strip()
        year = m.get("ProductionYear")
        path = m.get("Path") or ""
        label = f"  [{i}/{len(without_id)}] {title} ({year})"

        # 1. A previous run may have already stamped an ID into the filename.
        from_name = id_in_filename(path)
        if from_name:
            resolved[m["Id"]] = from_name
            log(f"{label} -> {from_name}  (from filename)")
            continue

        # 2. Ask TMDb for an IMDb ID.
        try:
            imdb, tmdb_from_search, note = tmdb.find_ids(title, year)
        except Exception as e:
            imdb, tmdb_from_search, note = None, None, f"lookup error: {e}"

        if imdb:
            resolved[m["Id"]] = f"imdbid-{imdb}"
            log(f"{label} -> imdbid-{imdb}  [{note}]")
            continue

        # 3. No IMDb ID anywhere. Fall back to a TMDb ID — Jellyfin reads
        #    [tmdbid-NNNN] from filenames exactly like [imdbid-ttNNNN].
        #    Prefer the ID Jellyfin already holds over the one we searched for.
        tmdb_id = existing_tmdb(m) or tmdb_from_search
        if tmdb_id:
            resolved[m["Id"]] = f"tmdbid-{tmdb_id}"
            via_tmdb_id += 1
            src = "from Jellyfin" if existing_tmdb(m) else "from TMDb search"
            log(f"{label} -> tmdbid-{tmdb_id}  (no IMDb ID exists; {src})")
            continue

        unmatched.append((title, year, path, note))
        log(f"{label} -> NO MATCH  [{note}]")

    # ── Phase 4: report ──────────────────────────────────────────────────────
    log("\n[4/5] Unmatched movies")
    log("-" * 70)
    if not unmatched:
        log("      None — every movie has a usable ID.")
    else:
        for title, year, path, reason in unmatched:
            log(f"  {title} ({year or '????'})")
            log(f"      reason: {reason}")
            log(f"      path:   {path}")
    log("-" * 70)
    log(f"      Resolved: {len(resolved)}  (of which {via_tmdb_id} via TMDb ID)")
    log(f"      Unmatched: {len(unmatched)}")

    # ── Phase 5: rename ──────────────────────────────────────────────────────
    renamed = skipped = errored = 0

    if args.no_rename:
        log("\n[5/5] Renaming skipped (--no-rename).")
    else:
        log(f"\n[5/5] {'Renaming' if apply_changes else 'Planning renames'}...")
        candidates = [m for m in movies
                      if (existing_imdb(m) or resolved.get(m["Id"])) and m.get("Path")]
        total = len(candidates)
        already_ok = 0

        for n, m in enumerate(movies, 1):
            title = m.get("Name", "").strip()
            year = m.get("ProductionYear")
            imdb = existing_imdb(m)
            tag = f"imdbid-{imdb}" if imdb else resolved.get(m["Id"])
            jf_path = m.get("Path")

            if not tag:
                skipped += 1
                continue
            if not jf_path:
                log(f"  {title}: no path reported by Jellyfin, skipping.")
                skipped += 1
                continue

            # Live progress so a slow share never looks like a freeze.
            print(f"  [{n}/{len(movies)}] {title[:50]}...".ljust(78)[:78],
                  end="\r", flush=True)

            local = translate_path(jf_path)
            new_stem = target_stem(title, year, tag)

            try:
                pairs = plan_renames(local, new_stem)
            except FileNotFoundError:
                print(" " * 78, end="\r")
                log(f"  {title}:")
                log(f"    !! FILE NOT FOUND: {local}")
                log(f"       (Jellyfin reported: {jf_path} — check PATH_MAP)")
                errored += 1
                continue

            if not pairs:
                already_ok += 1
                if RENAME_PARENT_FOLDER:
                    maybe_rename_folder(local, title, year, apply_changes, log)
                continue

            print(" " * 78, end="\r")
            log(f"  {title} ({year}) [{tag}]")
            execute_renames(pairs, apply_changes, log)
            renamed += 1

            if RENAME_PARENT_FOLDER:
                maybe_rename_folder(local, title, year, apply_changes, log)

        print(" " * 78, end="\r")
        log(f"      {already_ok} already correctly named.")

    # ── Summary + CSV ────────────────────────────────────────────────────────
    log("\n" + "=" * 70)
    log("SUMMARY")
    log(f"  Movies scanned:      {len(movies)}")
    log(f"  IMDb ID already set: {len(with_id)}")
    log(f"  Resolved this run:   {len(resolved)}")
    log(f"    via TMDb ID:       {via_tmdb_id}")
    log(f"  Could not match:     {len(unmatched)}")
    if not args.no_rename:
        verb = "Files renamed" if apply_changes else "Files to rename"
        log(f"  {verb}:{' ' * max(1, 20 - len(verb))}{renamed}")
        log(f"  Skipped:             {skipped}")
        log(f"  Path errors:         {errored}")
    if not apply_changes and not args.no_rename:
        log("\n  DRY RUN — nothing was changed. Re-run with --apply to commit.")

    if WRITE_REPORT_FILES:
        with open(args.report, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["status", "title", "year", "provider_tag", "reason", "path"])
            for m in movies:
                imdb = existing_imdb(m)
                if imdb:
                    w.writerow(["had_imdb", m.get("Name"), m.get("ProductionYear"),
                                f"imdbid-{imdb}", "", m.get("Path")])
                elif m["Id"] in resolved:
                    tag = resolved[m["Id"]]
                    status = "resolved_tmdb" if tag.startswith("tmdbid-") else "resolved_imdb"
                    w.writerow([status, m.get("Name"), m.get("ProductionYear"),
                                tag, "", m.get("Path")])
            for title, year, path, reason in unmatched:
                w.writerow(["unmatched", title, year, "", reason, path])
        print(f"\n  Report written to {args.report}")

        with open("imdb_rename.log", "w", encoding="utf-8") as f:
            f.write("\n".join(log_lines))
        print("  Log written to imdb_rename.log")

    if args.refresh and apply_changes:
        print("\nTriggering Jellyfin library scan...")
        try:
            jf.refresh_library()
            print("  Scan started.")
        except Exception as e:
            print(f"  Failed to start scan: {e}")


if __name__ == "__main__":
    main()
