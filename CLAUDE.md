# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Two Python CLI scripts sharing one project:

- **[album_downloader.py](album_downloader.py)** — takes an artist + album name, looks up the official tracklist and cover art on Spotify, downloads matching audio from YouTube, and writes fully-tagged mp3 files into a per-album folder.
- **[sync_missing.py](sync_missing.py)** — scans your Spotify library for tracks that are region-locked in your market and not yet downloaded, and produces a queue that `album_downloader.py` can consume.

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

The pipeline is linear, all in `download_album()`:

1. **Metadata lookup** (`get_spotify_client`, `fetch_album_metadata`) — spotipy searches Spotify for `album:X artist:Y` with `market="US"` (a market is required here — without one, Client Credentials search/track-listing calls return empty or unreliable results), takes the first match, and pulls track numbers/titles/cover URL. This is the source of truth for filenames and tags, independent of what actually gets downloaded from YouTube.
2. **Folder setup** — output goes to `BASE_MUSIC_PATH\<Artist> - <Album>\` (`BASE_MUSIC_PATH` is a hardcoded constant at the top of the file — update it if it doesn't match the actual local username/path). Cover art is saved once as `00 cover.jpg` and reused for every track's embedded artwork.
3. **Per-track download** (`download_track_audio`) — yt-dlp runs a `ytsearch1:` query against `"<Artist> - <Title> (Audio)"` and transcodes the top hit to mp3. Audio source (YouTube) is decoupled from metadata source (Spotify), so tagging always uses the Spotify-derived values, not anything read from the downloaded file.
4. **Tagging** (`tag_mp3`) — any existing ID3 tag on the downloaded file is deleted first, then a clean tag is written (`TIT2`/`TPE1`/`TALB`/`TRCK` + `APIC` cover). This avoids mixing yt-dlp/YouTube-sourced metadata with the Spotify-sourced tags.

Filenames use `sanitize_filename()` for both the album folder name and each track filename — it replaces path separators with `-` and strips other Windows-illegal characters (`:*?"<>|`). Any code path that constructs a filesystem path from Spotify-provided strings should go through this function (both scripts do).

Each track is wrapped in its own try/except in the main loop so one failed download or tag operation doesn't abort the rest of the album; failures are printed with `[Error]` and the loop continues.

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
