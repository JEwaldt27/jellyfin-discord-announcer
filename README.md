# Jellyfin → Discord announcer

Watches your Jellyfin server and posts a Discord embed for every movie and TV
season that shows up. Also renames movie files to match their IMDb IDs, on
demand, from a slash command.

- **Movies** get one post each: poster, backdrop, overview, runtime, rating, genres.
- **TV** is grouped per season — a 10-episode drop is *one* post, not ten.
- Artwork is uploaded with the message, so it works with a LAN-only Jellyfin
  that Discord's servers can't reach.
- **`/imdb`** matches movies to IMDb IDs and renames the files to
  `Title (Year) [imdbid-tt0110912].mkv`, which is the naming Jellyfin
  identifies most reliably.

---

## Commands

Run `/help` in Discord for this same list, annotated with what you personally
can use. All replies are private (only you see them).

| Command | What it does | Who can use it |
|---|---|---|
| `/help` | List every command and the roles they need | anyone |
| `/status` | Settings, last/next scan, item counts, Jellyfin health | anyone |
| `/start` | Run a scan right now | anyone |
| `/channels movies <#channel>` | Where new movies get announced | **role** |
| `/channels shows <#channel>` | Where new TV episodes get announced | **role** |
| `/scanrate <interval>` | How often to scan: `30m`, `6h`, `24h`, `7d`. Default 24h, min 15m | **role** |
| `/imdb [apply] [limit]` | Match movies to IMDb IDs and rename the files | **role** |
| `/rebaseline confirm:True` | Forget what's been announced, re-record the library as the new baseline | **role** |

**role** = the Discord role named in `ADMIN_ROLE` (default `Media Admin`).

Anything that changes state needs the role; read-only commands are open. This
is enforced *by the bot*, which means it also applies to server administrators
and cannot be overridden from Discord's **Integrations** settings.

---

## What you need before starting

- A machine that runs Docker (the "bot host"). It does **not** have to be the
  Jellyfin box.
- **Jellyfin 12.0 or newer**, reachable from the bot host. See
  [Jellyfin version notes](#jellyfin-version-notes) below.
- **For `/imdb` only:** the movie files mounted on the bot host — SMB, NFS,
  local disk, anything — with write access. Skip this and everything except
  `/imdb` still works.

---

## Setup

### 1. Create the Discord bot

1. <https://discord.com/developers/applications> → **New Application**, name it
   whatever you like.
2. **Bot** tab → **Reset Token** → copy it. This is your `DISCORD_TOKEN`.
   Treat it like a password — anyone holding it controls the bot.
3. Leave all three Privileged Gateway Intents **off**. This bot doesn't read
   messages and doesn't need them.
4. **OAuth2** tab → copy your **Client ID**, then open this URL with your ID
   substituted in:

   ```
   https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&permissions=52224&scope=bot%20applications.commands
   ```

   That grants exactly: View Channel, Send Messages, Embed Links, Attach Files.
   Pick your server and authorize.

### 2. Create the admin role in Discord

Server Settings → **Roles** → create a role named **`Media Admin`**, and assign
it to yourself and anyone else who should configure the bot or rename files.

Prefer a role you already have? Put its exact name in `ADMIN_ROLE` in step 4
instead. The name must match exactly, including capitalisation.

### 3. Get a Jellyfin API key

Jellyfin **Dashboard → Advanced → API Keys → +**, name it `discord-bot`, copy
the key. This is your `JELLYFIN_API_KEY`.

### 4. Fill in `.env`

```bash
git clone <this repo> jellyfin-discord-bot
cd jellyfin-discord-bot
cp .env.example .env
```

Open `.env`. Every setting is documented there; these are the ones that matter:

| Variable | Required | What it is |
|---|---|---|
| `DISCORD_TOKEN` | **yes** | From step 1 |
| `JELLYFIN_URL` | **yes** | Address the *bot* uses to reach Jellyfin. A LAN address is fine |
| `JELLYFIN_API_KEY` | **yes** | From step 3 |
| `ADMIN_ROLE` | no | Role required for the privileged commands. Default `Media Admin` |
| `JELLYFIN_PUBLIC_URL` | no | External address, if you have one. Adds an "open in Jellyfin" link to each post. Blank = no link |
| `MOVIES_JELLYFIN_PATH` | for `/imdb` | The path prefix **Jellyfin** reports for movies |
| `MOVIES_LOCAL_PATH` | for `/imdb` | Where those same files are mounted on the **bot host** |
| `TMDB_API_KEY` | no | Only for movies with no IMDb ID anywhere. See step 6 |
| `PUID` / `PGID` | yes-ish | Must match `id -u` and `id -g` on the bot host, or `./data` won't be writable |
| `MAX_POSTS_PER_SCAN` | no | Cap per scan, default 20 |
| `LOG_LEVEL` | no | `INFO` normally, `DEBUG` is chatty |

### 5. Point the bot at your movie files (for `/imdb`)

Skip this if you don't want `/imdb`. Everything else works without it.

The two path variables must describe the **same files** from two points of view.

**The Jellyfin side.** Open any movie in Jellyfin and look at its file path.
If it reads `/movies/Heat (1995).mkv`, then:

```
MOVIES_JELLYFIN_PATH=/movies
```

**The local side.** Wherever those files are mounted on the bot host:

```
MOVIES_LOCAL_PATH=/mnt/movies
```

These are often different, because Jellyfin usually runs in its own container
with its own mount points. Setting `MOVIES_LOCAL_PATH` is all you need to do —
`docker-compose.yml` reads it from `.env` and mounts the share into the bot's
container at the same path automatically.

> **This gives the bot write access to your media files.** That's what makes
> renaming possible. To keep the bot read-only, add `:ro` to the end of the
> movies volume line in `docker-compose.yml`. `/imdb` will still scan and
> report; only `apply: True` will fail.

**Is your library flat or foldered?** This script assumes **flat** — movie
files sitting directly in one directory:

```
/mnt/movies/Heat (1995).mkv
/mnt/movies/Alien (1979).mkv
```

If yours uses a folder per movie (`/mnt/movies/Heat (1995)/Heat (1995).mkv`),
the file gets renamed correctly but the *folder* keeps its old name. To rename
folders too, set `RENAME_PARENT_FOLDER = True` near the top of
`jellyfin_imdb_rename.py`. There's a guard that refuses to rename a share root,
but read that section before turning it on.

### 6. Optional: a TMDb key

Only needed for movies where Jellyfin has **no IMDb ID at all**. If your
library is already well-identified you can skip this entirely — `/imdb` will
tell you if it ever needs one.

Free key at <https://www.themoviedb.org/settings/api> — the **API Key (v3
auth)**, the 32-character one, not the long "API Read Access Token".

```bash
./add_tmdb_key.sh          # prompts, input hidden
./add_tmdb_key.sh <key>    # non-interactive
```

It writes `TMDB_API_KEY` to `.env` and verifies the key against TMDb before
declaring success.

### 7. Start it

```bash
docker compose up -d --build
docker compose logs -f
```

You want to see:

```
Connected to Discord as YourBot#1234
Jellyfin reachable: 'your-server' (version 10.x.x)
Slash commands registered in Your Server
```

If it warns `has no 'Media Admin' role`, go back to step 2 — nobody will be
able to use the privileged commands until that role exists.

### 8. Configure it in Discord

Slash commands register per-guild, so they appear immediately.

```
/channels movies #new-movies
/channels shows #new-shows
/scanrate 24h
/start
```

**The first scan posts nothing.** It records your existing library as the
baseline so the bot doesn't dump your whole collection into the channel.
Everything added after that gets announced.

---

## Using `/imdb`

```
/imdb                 → dry run over the whole library, changes nothing
/imdb limit: 20       → dry run, first 20 movies only
/imdb apply: True     → actually rename the files
```

**Dry run by default.** Nothing is renamed unless you explicitly pass
`apply: True`. Run it plain first and read the report.

What it does, in order:

1. Triggers a Jellyfin library scan and waits for it to finish, so it works
   from current metadata rather than a stale cache.
2. Fetches every movie.
3. For any movie with no IMDb ID: checks the filename for an existing
   `[imdbid-…]` or `[tmdbid-…]` tag, then Jellyfin's own TMDb ID, then asks
   TMDb (if you set a key).
4. Reports anything it couldn't match.
5. Renames media files and their sidecars (subtitles, `.nfo`, artwork) to
   `Title (Year) [imdbid-tt0110912].mkv`.

The reply is an embed with the summary numbers and the full run log — the same
output the script prints on a terminal. If the log is too long for one embed
it's attached as a file.

A movie with no IMDb ID anywhere falls back to `[tmdbid-12345]`, which Jellyfin
reads just as happily.

---

## Jellyfin version notes

Developed and tested against **Jellyfin 12.0.0**.

Both the announcer and `/imdb` pass `collapseBoxSetItems=false` on every item
query. This matters if you use collections: Jellyfin 12 changed the default,
and without it a `Movie` query returns the **collection** in place of every
movie inside it. On a library with 21 genre collections that turns 383 movies
into 21 collections plus the handful belonging to no collection — which means
the announcer posts your collections as if they were new films, real new
movies go unnoticed, and `/imdb` reports `FILE NOT FOUND` for every collection
because a collection has no media file.

`/imdb` additionally discards anything whose type isn't `Movie`, or that is
flagged as a folder, before it reaches the rename step. The query shouldn't
return such an item, but renaming a directory isn't a failure worth trusting a
server-side filter to prevent.

Older Jellyfin releases aren't tested. `collapseBoxSetItems` is a long-standing
API parameter, so 10.x will likely work, but that's untested rather than
supported.

---

## How it decides something is new

Each scan walks Jellyfin newest-first and compares item IDs against a
`seen_items` table in SQLite, paging until it hits a page where *every* item is
already known.

It deliberately doesn't trust `DateCreated` alone: Jellyfin rewrites that
timestamp when a library rescan re-identifies a file, which would announce the
same movie twice — or, if the walk stopped at the first familiar item, hide
genuinely new media sitting below it.

An item is marked seen only once it has been posted successfully. If Jellyfin
is down, a channel isn't set, or the bot can't post, nothing is recorded and it
retries next scan.

## Behaviour worth knowing

- **Post cap.** `MAX_POSTS_PER_SCAN` (default 20) limits one scan. Anything
  past the cap is marked seen and summarised in a single "…and N more" post, so
  a bulk import can't produce 200 messages.
- **Failed scans retry sooner** — 15 minutes instead of a full cycle.
- **The schedule survives restarts.** Next scan time lives in the database.
- **Per-guild settings.** Channels, scan rate and seen-items are tracked per
  Discord server, so one bot can serve several.

## Day to day

```bash
docker compose logs -f              # follow logs
docker compose restart              # restart
docker compose down                 # stop
docker compose up -d --build        # apply code changes
cp data/bot.db data/bot.db.backup   # back up state
```

Logs are capped at 3 × 10 MB by the compose file, so they can't quietly eat the
disk.

---

## Troubleshooting

**Commands don't appear in Discord.** The bot must have been invited with the
`applications.commands` scope — re-run the invite URL in step 1. Check the logs
for `Slash commands registered`.

**"You need the Media Admin role to use this."** Working as intended. Assign
the role (step 2), or change `ADMIN_ROLE` in `.env` and restart. Being a server
admin does *not* bypass this.

**Logs warn `has no 'Media Admin' role`.** The role doesn't exist in that
server, so nobody can use the privileged commands. Create it, or fix the name.

**"Jellyfin rejected the API key (401)."** Wrong or revoked
`JELLYFIN_API_KEY`. Generate a new one.

**"Cannot reach Jellyfin."** From the bot host:
`curl http://YOUR_JELLYFIN:PORT/System/Info/Public`. If Jellyfin runs in Docker
on the same box, `localhost` inside the bot's container is *not* the host — use
the LAN IP, or put both on the same Docker network.

**`/imdb` says "Cannot reach /mnt/movies".** `MOVIES_LOCAL_PATH` is wrong, or
the mount isn't up on the host. Check with `mount | grep movies`, then
`docker compose up -d` to re-create the container with the corrected path.

**`/imdb` reports "FILE NOT FOUND" for every movie.** `MOVIES_JELLYFIN_PATH`
doesn't match what Jellyfin actually reports. The error line shows the path
Jellyfin gave — set the variable to that prefix.

**`/imdb` says "No write permission".** The mount is read-only, or `PUID`/`PGID`
don't match the owner of the files. Dry runs still work.

**Posts appear without images.** The bot reached Jellyfin's API but not its
image endpoint, or the item has no artwork. Set `LOG_LEVEL=DEBUG` and restart.

**It announced my whole library.** The baseline didn't run — that only happens
if the database was wiped. `docker compose down`, restore `data/bot.db`, or run
`/rebaseline confirm: True`.

**It's posting nothing at all.** Run `/status`. It shows whether Jellyfin is
reachable, when the last scan ran, what it found, and whether both channels are
set.

---

## Running without Docker

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
DB_PATH=./data/bot.db python bot.py
```

`bot.py` reads `.env` automatically via python-dotenv when it's present. Set
`MOVIES_LOCAL_PATH` to wherever the files actually are on that machine.

The rename script also runs standalone, with no third-party dependencies:

```bash
python3 jellyfin_imdb_rename.py                 # dry run
python3 jellyfin_imdb_rename.py --limit 20      # dry run, first 20
python3 jellyfin_imdb_rename.py --apply         # rename for real
python3 jellyfin_imdb_rename.py --no-scan       # skip the pre-run library scan
python3 jellyfin_imdb_rename.py --refresh       # trigger a Jellyfin scan when done
```

Set `WRITE_REPORT_FILES = True` at the top of that file if you want
`imdb_report.csv` and `imdb_rename.log` written to disk; it's off by default
and everything is printed to the terminal either way.

---

## What's in here

| File | Role |
|---|---|
| `bot.py` | Discord client, slash commands, scan scheduler |
| `scanner.py` | Walks Jellyfin, diffs against seen items, posts |
| `jellyfin.py` | Jellyfin API client |
| `db.py` | SQLite state — guild settings and seen items |
| `embeds.py` | Builds the announcement embeds |
| `jellyfin_imdb_rename.py` | The `/imdb` worker. Runs standalone too |
| `add_tmdb_key.sh` | Writes and verifies `TMDB_API_KEY` in `.env` |

`.env` and `data/` hold your token, API keys and database. Both are gitignored
— **never** copy them to anyone else.

---

## License

MIT — see [LICENSE](LICENSE). Do what you like with it.
