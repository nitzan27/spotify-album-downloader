# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two Python CLI scripts plus a small web app, sharing one project:

- **[album_downloader.py](album_downloader.py)** — takes an artist + album name, looks up the official tracklist and cover art on Spotify, downloads matching audio from YouTube, and writes fully-tagged mp3 files into a per-album folder. Its core `download_album()` pipeline is also reused by the web app (see below).
- **[sync_missing.py](sync_missing.py)** — scans your Spotify library for tracks that are region-locked in your market and not yet downloaded, and produces a queue that `album_downloader.py` can consume.
- **[web/](web/)** — a FastAPI + React/TypeScript web app (friends/invite-only — access control is just not sharing the URL, no site-wide login) that lets other people submit artist/album downloads (delivered as a zip, or saved straight into a local folder they pick — see `web/frontend/src/downloadFolder.ts`), or log into their **own** Spotify account for a personalized region-lock scan (reusing `sync_missing.py`'s logic, adapted to run per-user on a server — see `web/scan_adapter.py`). Deployed to Render; see `web/CLAUDE.md` for architecture and deployment details.

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
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8080/callback       # only needed by sync_missing.py
SPOTIFY_WEB_REDIRECT_URI=http://127.0.0.1:8000/callback   # only needed by web/ (its own callback, not the above)
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

**Errors are raised, not `sys.exit()`'d, from inside the pipeline.** `get_spotify_client()` and `fetch_album_metadata()` raise `SpotifyLookupError` (a plain `Exception`) instead of calling `sys.exit(1)` — this only matters because `download_album()` is reused from a non-main thread by the web app's job worker, where `sys.exit()` would silently kill just that thread and leave a job hung forever instead of surfacing an error. The CLI's `__main__` block catches `SpotifyLookupError` and does the `print` + `exit(1)` there instead, preserving the original console behavior. A Spotify 429 specifically is caught and turned into a short "Spotify rate limit reached" message rather than hanging — raised as `SpotifyRateLimitError`, a `SpotifyLookupError` subclass carrying the parsed `Retry-After` delay as a typed field (`_spotify_lookup_error()` builds either it or a plain `SpotifyLookupError` from the raw `SpotifyException`) so a caller that wants to auto-retry doesn't have to string-parse the message; the web app's `downloader_adapter.py` does exactly that (see `web/CLAUDE.md`) since a burst of concurrent downloads can trip the same shared rate limit scans already had auto-retry for. `get_spotify_client()` sets `retries=0, status_retries=0` for the same reason `sync_missing.py` does (see below): spotipy's default retry behavior sleeps for the server's `Retry-After` duration, which can be hours, and `retries=0` alone doesn't fully disable that (see the rate-limit note below — `status_retries` is a separate counter).

**`progress_callback(event, **data)`** decouples progress reporting from `print()`. The default (`_default_progress_callback`) reproduces the CLI's original console output exactly. The web app passes its own callback that writes into a `Job.progress` string instead (see `web/downloader_adapter.py`). Events: `fetching_metadata`, `output_folder`, `track_start`, `track_done` (`success`/`error` kwargs), `done`.

## sync_missing.py (region-lock sync)

Scans the logged-in user's saved albums + liked songs + playlists for tracks unavailable in their market (`DEFAULT_MARKET`, currently `"IL"`), cross-references against `BASE_MUSIC_PATH`, and writes/prints a sync queue rather than downloading directly. Its `sp`-parameterized helpers (`_gather_library_albums`, `_track_key`, `_chunked`, `_fetch_album_tracks_batch`, `_locked_keys_for_batch`, `_load_cache`/`_save_cache`) are also reused directly by the web app's `web/scan_adapter.py` to run the same scan per-user on a server — see `web/CLAUDE.md`.

```
python sync_missing.py            # scan, print, and save the queue to sync_queue.json
python sync_missing.py --download # same, then immediately download everything queued
python album_downloader.py sync_queue.json   # inspect/edit the queue first, then download it separately
```

**Auth is different from the main script.** `album_downloader.py` uses Client Credentials (app-only, no `/me/*` access), but reading a user's library requires Authorization Code with real scopes (`get_user_spotify_client`). `sync_missing.py` opens its own browser login on first run and caches the token in `.spotify_user_cache` (gitignored). This requires `SPOTIFY_REDIRECT_URI` to also be registered as an exact-match Redirect URI on the Spotify app in the developer dashboard.

**Detecting region-lock without false positives.** Spotify sometimes swaps in a new track ID for the same song (explicit-version/copyright re-uploads, common on hip-hop albums) independent of actual market availability. Comparing raw track IDs between an unrestricted `album_tracks` call and a market-scoped one therefore produces false "region-locked" flags. `_track_key()` instead identifies a track by `(disc_number, track_number, sanitized lowercase name)`, which stays stable across those re-uploads; `_locked_keys_for_batch()` diffs on this key, not on ID.

**Real libraries contain messier data than a catalog search result.** `_album_meta()` (and the gather functions that call it) defensively handle albums with an empty/missing `artists` list and skip local files entirely (`album.get("id")` falsy) — unlike an `album_downloader.py` catalog search result, a real saved-tracks/playlist library can contain local uploads or other incomplete entries that have no real artist or catalog id. Crashed a live scan with `IndexError: list index out of range` before this was added; local files also can't be region-lock-checked via `sp.albums()` anyway (no catalog id), so they're dropped rather than defused.

**A durable cache makes repeat scans cheap, not just a single scan reliable.** The old design only checkpointed the region-lock-check phase and threw that checkpoint away on success, so a routine re-scan of an already-scanned, unchanged library cost exactly as much as the first scan — on a large account (400+ saved albums, thousands of liked songs, 90+ playlists) that alone was enough to trip Spotify's rate limiter *every time you ran it*, not just once. `.spotify_scan_cache.json` (`CACHE_PATH`, gitignored) fixes this by never being deleted and being updated incrementally as the scan progresses:
- **Saved albums** (`_gather_saved_albums`): `/me/albums` returns newest-added-first, so pagination stops as soon as it reaches an album id already in the cache — only genuinely new saves cost a call.
- **Liked songs** (`_gather_liked_song_albums`): `/me/tracks` ("Liked Songs" — distinct from saved *albums*, and previously not scanned at all) is also newest-saved-first, so the same early-stop trick applies, just keyed by the saved track's id instead of an album id (several liked songs commonly share one album). A large Liked Songs library can span hundreds of pages, so an in-progress walk also resumes via an `offset` after a crash instead of restarting at page 1 — but that resume offset (`in_progress_offset`) is kept deliberately separate from `newest_seen_track_id` (the "stop early on the *next* routine re-scan" marker), which is only ever set once a walk has verifiably reached the true bottom of the list. Conflating the two was an actual bug caught during development: a crash mid-walk on the very first (cold) scan would otherwise make the next run believe everything past the crash point was already recorded, silently dropping a chunk of the library instead of just costing a redo.
- **Playlists** (`_gather_playlist_albums`): each playlist's Spotify `snapshot_id` (bumped whenever its contents change) is compared to the cached value; an unchanged playlist's tracks are never re-fetched, and the cache is saved after every playlist (not just at the end), so an interrupted scan only redoes the playlist it was on — this is exactly what a real 502 storm hit partway through playlist ~90 of 95.
- **Region-lock status** (`_is_lock_status_stale`, `LOCK_STATUS_TTL_DAYS = 30`): each album's lock-check result is cached for 30 days (lock status rarely changes), so most albums skip the 2-call `_locked_keys_for_batch()` lookup entirely on a repeat scan — this is the single biggest lever, since it's the most expensive phase.

Because every unit of work is saved to the cache as it happens, this cache *is* the resume mechanism too — there's no separate in-memory "checkpoint" concept. Re-running after an interruption and re-running for a routine repeat scan are the same code path; the cache dict just already has more in it the second time. Known, accepted inaccuracy: an album you later *unsave*, or a song you *unlike*, lingers in the cached id list forever (costs a wasted lock-check every ~30 days, never an incorrect result) — not worth solving given how rarely it matters. Both `user-library-read` reads (`/me/albums` and `/me/tracks`) are covered by the same OAuth scope already requested (`USER_SCOPE`), so adding liked songs needed no scope/re-auth change.

**Rate-limit / error handling.** The client is constructed with `retries=0, status_retries=0` so a 429 (or a run of 502s) raises immediately as `SpotifyException` instead of spotipy auto-sleeping. **Both matter**: `retries=0` only zeroes urllib3's overall `total` retry budget — status-code retries (429/500/502/503/504) are tracked by a *separate* `status` counter that defaults to 3 regardless of `retries`, so leaving `status_retries` unset still silently retries a few times with backoff before finally raising (confirmed live: a 502 storm scanning a 400+ album/95-playlist library via the web app cost real time before failing, prompting this fix — see `web/CLAUDE.md`). `scan_region_locked_albums()` treats 429 *and* 500/502/503/504 alike (`_RECOVERABLE_HTTP_STATUSES`) as "save the cache and stop cleanly" rather than letting only 429 be recoverable and everything else crash uncaught.

`sync_queue.json` (gitignored) is the human-facing output — a list of `{"artist", "album", "missing_titles"}` — meant to be inspected/edited before feeding it to `album_downloader.py`.

