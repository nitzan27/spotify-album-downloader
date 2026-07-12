# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two Python CLI scripts plus a small web app, sharing one project:

- **[album_downloader.py](album_downloader.py)** — takes an artist + album name, looks up the official tracklist and cover art on Spotify, downloads matching audio from YouTube, and writes fully-tagged mp3 files into a per-album folder. Its core `download_album()` pipeline is also reused by the web app (see below).
- **[sync_missing.py](sync_missing.py)** — scans your Spotify library for tracks that are region-locked in your market and not yet downloaded, and produces a queue that `album_downloader.py` can consume.
- **[web/](web/)** — a FastAPI web app (friends/invite-only, gated by a shared passcode) that lets other people submit artist/album downloads and get a zip back, without needing their own machine set up. Deployed to Render; see "web/ (multi-user web app)" below.

## Running

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
python album_downloader.py
```

Requires `ffmpeg` on PATH (used by yt-dlp to transcode to mp3). Credentials live in `.env` (gitignored), loaded automatically via `python-dotenv`:

```
SPOTIFY_CLIENT_ID=...
SPOTIFY_CLIENT_SECRET=...
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8080/callback   # only needed by sync_missing.py
```

Free credentials from https://developer.spotify.com/dashboard. There is no test suite, linter, or build step in this repo.

## album_downloader.py

Two ways to run it:

- `python album_downloader.py` — interactive prompt for artist + album name (original behavior).
- `python album_downloader.py <queue.json>` — reads a JSON file (a list of `{"artist", "album", ...}` objects, e.g. the sync queue `sync_missing.py` produces) via `download_from_queue_file()` and downloads every album in it with no prompts.

The pipeline is linear, all in `download_album(artist_name, album_name, dest_root=BASE_MUSIC_PATH, progress_callback=_default_progress_callback)`:

1. **Metadata lookup** (`get_spotify_client`, `fetch_album_metadata`) — spotipy searches Spotify for `album:X artist:Y` with `market="US"` (a market is required here — without one, Client Credentials search/track-listing calls return empty or unreliable results), takes the first match, and pulls track numbers/titles/cover URL. This is the source of truth for filenames and tags, independent of what actually gets downloaded from YouTube.
2. **Folder setup** — output goes to `<dest_root>\<Artist> - <Album>\` (`dest_root` defaults to `BASE_MUSIC_PATH`, a hardcoded constant at the top of the file for the CLI's own use — update it if it doesn't match the actual local username/path; the web app passes its own per-job temp directory instead, see below). Cover art is saved once as `00 cover.jpg` and reused for every track's embedded artwork.
3. **Per-track download** (`download_track_audio`) — yt-dlp runs a `ytsearch1:` query against `"<Artist> - <Title> (Audio)"` and transcodes the top hit to mp3. Audio source (YouTube) is decoupled from metadata source (Spotify), so tagging always uses the Spotify-derived values, not anything read from the downloaded file. `ydl_opts` sets `extractor_args: {youtube: {player_client: [android, ios, web]}}` — the web client's extraction path triggers YouTube's "Sign in to confirm you're not a bot" check much more readily from datacenter IPs (confirmed live on Render: every track failed until this was added) than from residential ones; falling back through the android/ios clients avoids that check as of now. This is a cat-and-mouse mitigation, not a permanent fix — if YouTube changes behavior again, this is the first place to look.
4. **Tagging** (`tag_mp3`) — any existing ID3 tag on the downloaded file is deleted first, then a clean tag is written (`TIT2`/`TPE1`/`TALB`/`TRCK` + `APIC` cover). This avoids mixing yt-dlp/YouTube-sourced metadata with the Spotify-sourced tags.

Filenames use `sanitize_filename()` for both the album folder name and each track filename — it replaces path separators with `-` and strips other Windows-illegal characters (`:*?"<>|`). Any code path that constructs a filesystem path from Spotify-provided strings should go through this function (both scripts do).

Each track is wrapped in its own try/except in the main loop so one failed download or tag operation doesn't abort the rest of the album; failures are reported through `progress_callback("track_done", success=False, error=...)` and the loop continues. `download_album()` returns the destination folder path.

**Errors are raised, not `sys.exit()`'d, from inside the pipeline.** `get_spotify_client()` and `fetch_album_metadata()` raise `SpotifyLookupError` (a plain `Exception`) instead of calling `sys.exit(1)` — this only matters because `download_album()` is reused from a non-main thread by the web app's job worker, where `sys.exit()` would silently kill just that thread and leave a job hung forever instead of surfacing an error. The CLI's `__main__` block catches `SpotifyLookupError` and does the `print` + `exit(1)` there instead, preserving the original console behavior. A Spotify 429 specifically is caught and turned into a short "Spotify rate limit reached" message (`_describe_spotify_exception`) rather than hanging — `get_spotify_client()` sets `retries=0` for the same reason `sync_missing.py` does (see below): spotipy's default retry behavior sleeps for the server's `Retry-After` duration, which can be hours.

**`progress_callback(event, **data)`** decouples progress reporting from `print()`. The default (`_default_progress_callback`) reproduces the CLI's original console output exactly. The web app passes its own callback that writes into a `Job.progress` string instead (see `web/downloader_adapter.py`). Events: `fetching_metadata`, `output_folder`, `track_start`, `track_done` (`success`/`error` kwargs), `done`.

## sync_missing.py (region-lock sync)

Scans the logged-in user's saved albums + playlists for tracks unavailable in their market (`DEFAULT_MARKET`, currently `"IL"`), cross-references against `BASE_MUSIC_PATH`, and writes/prints a sync queue rather than downloading directly.

```
python sync_missing.py            # scan, print, and save the queue to sync_queue.json
python sync_missing.py --download # same, then immediately download everything queued
python album_downloader.py sync_queue.json   # inspect/edit the queue first, then download it separately
```

**Auth is different from the main script.** `album_downloader.py` uses Client Credentials (app-only, no `/me/*` access), but reading a user's library requires Authorization Code with real scopes (`get_user_spotify_client`). `sync_missing.py` opens its own browser login on first run and caches the token in `.spotify_user_cache` (gitignored). This requires `SPOTIFY_REDIRECT_URI` to also be registered as an exact-match Redirect URI on the Spotify app in the developer dashboard.

**Detecting region-lock without false positives.** Spotify sometimes swaps in a new track ID for the same song (explicit-version/copyright re-uploads, common on hip-hop albums) independent of actual market availability. Comparing raw track IDs between an unrestricted `album_tracks` call and a market-scoped one therefore produces false "region-locked" flags. `_track_key()` instead identifies a track by `(disc_number, track_number, sanitized lowercase name)`, which stays stable across those re-uploads; `_locked_keys_for_batch()` diffs on this key, not on ID.

**Rate-limit handling.** Checking N albums naively costs 2 `album_tracks()` calls each; on a large library (400+ saved albums) this is enough to trip Spotify's rate limiter, and spotipy's default retry behavior silently sleeps for whatever `Retry-After` Spotify returns (can be hours). To avoid this:
- `_fetch_album_tracks_batch()` uses the "Get Several Albums" endpoint (`sp.albums()`, up to 20 IDs/call) instead of one call per album — about a 20x reduction in request volume.
- The client is constructed with `retries=0` so a 429 raises immediately as `SpotifyException` instead of spotipy auto-sleeping.
- `scan_region_locked_albums()` checkpoints progress (scanned album IDs + queue so far) to `.sync_scan_state.json` (gitignored) after every batch of 20, and on a 429 saves state and exits cleanly. Re-running the script resumes from the checkpoint instead of re-scanning everything.

`sync_queue.json` (gitignored) is the human-facing output — a list of `{"artist", "album", "missing_titles"}` — meant to be inspected/edited before feeding it to `album_downloader.py`.

## web/ (multi-user web app)

A FastAPI frontend so friends can use the downloader without setting up Python/ffmpeg/Spotify credentials themselves. Scope is deliberately narrow: **friends/invite-only, not public** — running a public YouTube-audio-ripping service for arbitrary strangers is a real copyright/ToS risk (cf. youtube-mp3.org-style shutdowns); this stays small and gated. Only the album-search-and-download flow is exposed; `sync_missing.py`'s per-user region-lock sync is not part of the web app.

**Run locally:**
```
.venv\Scripts\uvicorn web.app:app --reload --port 8000
```
Needs `SITE_USERNAME`/`SITE_PASSWORD` set (in `.env`) in addition to the Spotify credentials.

**Architecture** (all in `web/`):
- `jobs.py` — in-memory `Job` registry + a bounded `asyncio.Queue` with a small fixed-size pool (`WORKER_COUNT = 2`) of consumer tasks, so concurrent submissions don't spawn unbounded simultaneous yt-dlp/ffmpeg subprocesses. **Must run with a single uvicorn worker** (`--workers 1`) — job state lives in one process's memory; a second worker process would never see jobs created on the first. Expired jobs (`JOB_TTL_SECONDS`, 1 hour) are evicted by `cleanup_loop()`.
- `downloader_adapter.py` — bridges a `Job` to `album_downloader.download_album()`: builds a per-job temp dir (`TEMP_ROOT/<job_id>/<album folder>/` — the job_id segment means two concurrent downloads of the same album can't collide), passes a `progress_callback` that writes into `Job.progress`, zips the result, then deletes the uncompressed mp3s so only the zip remains on disk.
- `auth.py` — HTTP Basic Auth (`SITE_USERNAME`/`SITE_PASSWORD` env vars, `secrets.compare_digest`). Only meaningful over HTTPS (Render's free `*.onrender.com` domains provide this automatically).
- `app.py` — routes (`GET /`, `POST /jobs`, `GET /status/{id}`, `GET /download/{id}`) live on an `APIRouter` with the auth dependency applied at router level, so a newly added route is gated by construction rather than needing auth added per-route. Only `/healthz` (for Render's health checks) is unauthenticated — this also means a leaked `/download/{id}` URL alone isn't enough, since it still needs the passcode.
- `templates/index.html` — a single Jinja2 page: artist/album form → polls `/status/{id}` → reveals a download link.

**Deployment:** Render.com free "Web Service" tier, Docker-based (`Dockerfile` installs `ffmpeg` via apt, then `pip install -r requirements.txt`; `CMD` binds `0.0.0.0:$PORT` with `--workers 1`). Rejected alternatives: Fly.io no longer has a real free tier; Cloud Run only allocates CPU while a request is in flight by default, which conflicts with background job processing; a raw free VM (e.g. Oracle Always Free) is genuinely free but means self-managing an OS/Docker daemon, which is what hosting on Render avoids. Accepted trade-off: the free tier spins down after ~15 min idle (~30-60s cold start on the next request).

The Render service here (`spotify-album-downloader`, id `srv-d99me367r5hc73bf8qf0`) was created via the Render CLI against a **public** GitHub repo URL rather than through Render's GitHub App integration — which means **auto-deploy-on-push is not wired up** (Render can clone the public repo for a build, but has no webhook telling it a new commit landed). After pushing changes, deploy manually:
```
render deploys create srv-d99me367r5hc73bf8qf0 --confirm
```
(or reconnect the service via the Render dashboard's GitHub integration to get auto-deploy).

**Known limitations, accepted rather than solved:**
- In-memory job state means an in-progress job is lost if the free host restarts/spins down mid-job.
- Spotify Client Credentials rate limits are shared app-wide across every user of the site (same `SPOTIFY_CLIENT_ID` used by `album_downloader.py`'s CLI path and, historically, hit hard by `sync_missing.py`'s library scans) — fine at friend-group album-lookup volumes, but a single shared quota.
- Ephemeral disk means zips are transient by design (TTL cleanup), not persistent storage.
- YouTube's bot-check on datacenter IPs (see the `extractor_args` note above) is mitigated, not eliminated — if it resurfaces, check yt-dlp's changelog/issues for the current workaround before assuming the app is broken.

