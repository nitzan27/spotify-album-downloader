"""
Region-Lock Sync
=================

Scans your saved albums, liked songs, and playlists for tracks Spotify won't
let you play in your market, checks whether you already have them downloaded
locally, and queues up the rest for `download_album()` to fetch from YouTube
instead.

Why a separate user login
--------------------------
album_downloader.py authenticates with the Client Credentials flow, which is
app-only and has no access to anything under `/me/*` (saved albums, liked
songs, playlists). Reading a user's library requires the Authorization Code
flow with a real login + scopes, so this module opens its own browser-based
login the first time it runs and caches the resulting token in
`.spotify_user_cache`.

Why there's a durable scan cache
---------------------------------
On a large account (thousands of liked songs, hundreds of saved albums, 90+
playlists), a from-scratch scan costs a lot of Spotify API calls, and
re-running the scan later (to pick up a handful of new saves) used to cost
exactly as much as the first run - nothing was ever reused across runs.
`.spotify_scan_cache.json` (gitignored) fixes that: it's never deleted, and
every phase writes into it as it goes rather than starting over each time:
  - Saved albums and liked songs are both returned newest-first, so we stop
    paginating each as soon as we reach an item already in the cache - only
    genuinely new saves cost a call.
  - Each playlist's Spotify `snapshot_id` is compared to the cached one; an
    unchanged playlist's tracks are never re-fetched at all.
  - Each album's region-lock result is cached for `LOCK_STATUS_TTL_DAYS`
    (lock status rarely changes) so most albums skip the lock-check API
    calls entirely on a repeat scan.
Because every unit of work (one playlist, one batch of lock-checks) is saved
to the cache immediately, this cache doubles as the resume mechanism for an
interrupted scan (rate limit, Ctrl-C, crash) - there's no separate
"in-progress checkpoint" concept; resuming and reusing cached results across
runs are the same thing. This is a known, acceptable inaccuracy: an album you
later *unsave*, or a song you *unlike*, stays in the cached id list forever
(it just costs a wasted lock-check every ~30 days, never causes an incorrect
result).

One-time setup
---------------
1. In your Spotify Developer Dashboard app (the one SPOTIFY_CLIENT_ID already
   points to), add this exact Redirect URI:
       http://127.0.0.1:8080/callback
   (or set SPOTIFY_REDIRECT_URI in .env to whatever you register instead).
2. Run this module. A browser window will open asking you to log in and
   authorize; after that you'll land on a page that may 404 (there's no local
   server catching it) - copy the full URL from the address bar and paste it
   back into the terminal when spotipy asks for it. This only happens once;
   later runs reuse the cached token.

Usage
-----
    python sync_missing.py            # scan, print, and save the sync queue to sync_queue.json
    python sync_missing.py --download # same, then also download everything queued right away

Either way, the saved sync_queue.json can be inspected/edited by hand and fed
into the downloader later with:
    python album_downloader.py sync_queue.json
"""

import json
import os
import sys
import time
from datetime import date, timedelta

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from album_downloader import (
    BASE_MUSIC_PATH,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    AlbumDownloadError,
    SpotifyLookupError,
    download_album,
    sanitize_filename,
)

DEFAULT_MARKET = "IL"
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback")
USER_SCOPE = "user-library-read playlist-read-private playlist-read-collaborative"
TOKEN_CACHE_PATH = ".spotify_user_cache"
SYNC_QUEUE_PATH = "sync_queue.json"
CACHE_PATH = ".spotify_scan_cache.json"

# Batch size for the "Get Several Albums" endpoint (its hard max is 20). Using
# it instead of one album_tracks() call per album cuts request volume ~20x,
# which is what was tripping Spotify's rate limiter on a 400+ album library.
ALBUMS_BATCH_SIZE = 20
REQUEST_PAUSE_SECONDS = 0.2

# How long a cached region-lock result is trusted before we re-check it.
# Lock status rarely changes, so this is the single biggest lever for
# keeping repeat scans of a large, mostly-stable library cheap.
LOCK_STATUS_TTL_DAYS = 30

# Status codes retried by spotipy's default (disabled) retry adapter that we
# still want to treat as "recoverable, save progress and stop" rather than
# an uncaught crash - a 502 storm scanning a 400+ album library is what
# prompted this (see get_user_spotify_client()'s retries=0 comment below).
_RECOVERABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


def get_user_spotify_client() -> spotipy.Spotify:
    """Authenticate as the logged-in user (needed for saved albums/playlists)."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("[Error] Missing Spotify API credentials.")
        print("        Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET in .env.")
        sys.exit(1)

    auth_manager = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=USER_SCOPE,
        cache_path=TOKEN_CACHE_PATH,
    )

    if not os.path.exists(TOKEN_CACHE_PATH):
        print("[Info] No cached login found - a browser window will open for you to authorize.", flush=True)

    # retries=0: on a 429, spotipy's default retry adapter sleeps for whatever
    # Retry-After Spotify sends (which can be hours) before raising anything.
    # We'd rather it raise immediately so we can save progress and exit clean.
    # status_retries=0 too: retries=0 alone only zeroes urllib3's overall
    # `total` retry count - status-code retries (429/500/502/503/504) are a
    # separate counter that defaults to 3 regardless of `retries`, so a run
    # of 502s (as seen scanning a 400+ album library) would still silently
    # retry a few times with backoff before finally raising.
    return spotipy.Spotify(auth_manager=auth_manager, retries=0, status_retries=0)


def _paginate(sp: spotipy.Spotify, first_page: dict):
    """Yield every item across all pages of a spotipy paging object."""
    page = first_page
    while page:
        for item in page["items"]:
            yield item
        page = sp.next(page) if page.get("next") else None


def _album_meta(album: dict) -> dict:
    """Extract display metadata defensively - a real library can contain local
    files or other incomplete entries with an empty/missing `artists` list
    (unlike a catalog search result, which always has one) - crashed a real
    scan with "list index out of range" before this guard was added."""
    artists = album.get("artists") or []
    artist_name = artists[0]["name"] if artists else "Unknown Artist"
    return {"artist": artist_name, "album": album.get("name") or "Unknown Album"}


def _gather_saved_albums(sp: spotipy.Spotify, cache: dict, progress_cb) -> set[str]:
    """Return every saved-album id, reusing the cache to avoid re-paginating known ones.

    /me/albums returns newest-added-first, so we only need to paginate until
    we reach an album id we already saw on a previous run - everything after
    that point is guaranteed already known. Doesn't detect *unsaves*; a
    removed album just lingers in the cache (harmless - see module docstring).
    """
    saved_cache = cache["saved_albums"]
    previously_known_ids = saved_cache["album_ids"]
    newest_seen_id = saved_cache["newest_seen_id"]

    first_page = sp.current_user_saved_albums(limit=50)
    progress_cb(f"[Info] Found {first_page.get('total', 0)} saved album(s) total.")

    fresh_ids = []
    for item in _paginate(sp, first_page):
        album = item["album"]
        fresh_ids.append(album["id"])
        cache["albums_meta"][album["id"]] = _album_meta(album)
        if album["id"] == newest_seen_id:
            break

    all_ids = list(dict.fromkeys(fresh_ids + previously_known_ids))
    saved_cache["album_ids"] = all_ids
    saved_cache["newest_seen_id"] = all_ids[0] if all_ids else None
    return set(all_ids)


def _gather_liked_song_albums(sp: spotipy.Spotify, cache: dict, progress_cb) -> set[str]:
    """Return every album id referenced by a "Liked Songs" (saved track), cache-aware like saved albums.

    /me/tracks (unlike playlists) is also newest-saved-first, so the same
    early-stop pagination trick as _gather_saved_albums applies - just keyed
    by the saved *track's* id (each item here is one song, not one album, so
    several liked songs commonly share one album id). A large Liked Songs
    library can span hundreds of pages, so an in-progress walk also flushes
    an `offset` to resume from after every page - but that resume offset is
    kept strictly separate from `newest_seen_track_id` (the "stop early on
    the next routine re-scan" marker): the latter is only trusted once a
    walk has actually reached the true bottom of the list (or caught up to
    a *previous* fully-verified marker), never from wherever a walk merely
    happened to be interrupted. Conflating the two would mean a crash during
    the very first (cold) gather could make a later run believe everything
    past the crash point was already recorded, when it was never seen at
    all - silently dropping a chunk of the library rather than just costing
    an extra re-fetch. Doesn't detect unlikes; a removed liked song just
    lingers in the cache (harmless - see module docstring).
    """
    liked_cache = cache["liked_songs"]
    offset = liked_cache["in_progress_offset"]
    # Only a walk starting fresh from the top may use the early-stop
    # optimization - a resumed walk hasn't yet proven it's seen everything
    # between the resume point and that old marker, so it must walk for
    # real until it reaches the true bottom of the list.
    stop_at_track_id = liked_cache["newest_seen_track_id"] if offset == 0 else None
    walk_top_track_id = liked_cache["in_progress_top_track_id"]

    page = sp.current_user_saved_tracks(limit=50, offset=offset)
    progress_cb(f"[Info] Found {page.get('total', 0)} liked song(s) total.")

    while page:
        hit_stop = False
        for item in page["items"]:
            offset += 1
            track = item.get("track")
            if not track:
                continue
            # Local files (uploaded, not in the catalog) have id=None and
            # often album.id=None too - never usable as the resume cursor,
            # and can't be region-lock-checked via sp.albums() anyway.
            track_id = track.get("id")
            if walk_top_track_id is None and track_id is not None:
                walk_top_track_id = track_id
            if track_id is not None and stop_at_track_id is not None and track_id == stop_at_track_id:
                hit_stop = True
                break
            album = track.get("album")
            if album and album.get("id"):
                if album["id"] not in liked_cache["album_ids"]:
                    liked_cache["album_ids"].append(album["id"])
                cache["albums_meta"][album["id"]] = _album_meta(album)

        if hit_stop or not page.get("next"):
            # Either caught up to previously-verified territory, or reached
            # the true bottom of the list - either way this walk is now
            # fully verified from its start down to here.
            if walk_top_track_id is not None:
                liked_cache["newest_seen_track_id"] = walk_top_track_id
            liked_cache["in_progress_offset"] = 0
            liked_cache["in_progress_top_track_id"] = None
            break

        liked_cache["in_progress_offset"] = offset
        liked_cache["in_progress_top_track_id"] = walk_top_track_id
        _save_cache(cache)
        time.sleep(REQUEST_PAUSE_SECONDS)
        page = sp.next(page)

    return set(liked_cache["album_ids"])


def _gather_playlist_albums(sp: spotipy.Spotify, cache: dict, progress_cb) -> set[str]:
    """Return every album id referenced by any playlist track, reusing the cache per-playlist.

    Each playlist's Spotify `snapshot_id` changes whenever its contents
    change, so an unchanged playlist's tracks are never re-fetched. Saves the
    cache after every playlist (not just at the end) so an interrupted scan
    (rate limit, crash) only redoes the playlist it was on, not all of them -
    this is what a real 502 storm hit partway through playlist ~90 of 95.
    """
    playlists_cache = cache["playlists"]
    first_page = sp.current_user_playlists(limit=50)
    all_playlists = list(_paginate(sp, first_page))
    progress_cb(f"[Info] Found {len(all_playlists)} playlist(s) to check.")

    all_ids = set()
    seen_playlist_ids = set()
    for i, playlist in enumerate(all_playlists, start=1):
        seen_playlist_ids.add(playlist["id"])
        cached_entry = playlists_cache.get(playlist["id"])
        if cached_entry and cached_entry["snapshot_id"] == playlist.get("snapshot_id"):
            all_ids.update(cached_entry["album_ids"])
            continue

        progress_cb(f"[Info] ({i}/{len(all_playlists)}) Scanning playlist '{playlist['name']}'...")
        track_page = sp.playlist_items(playlist["id"], additional_types=("track",), limit=100)
        playlist_album_ids = []
        for item in _paginate(sp, track_page):
            track = item.get("track")
            # Local files (uploaded, not in the catalog) commonly have
            # album.id=None - skip them, they can't be region-lock-checked
            # via sp.albums() anyway.
            if not track or not track.get("album") or not track["album"].get("id"):
                continue
            album = track["album"]
            if album["id"] not in playlist_album_ids:
                playlist_album_ids.append(album["id"])
            cache["albums_meta"][album["id"]] = _album_meta(album)

        playlists_cache[playlist["id"]] = {
            "snapshot_id": playlist.get("snapshot_id"),
            "album_ids": playlist_album_ids,
        }
        all_ids.update(playlist_album_ids)
        _save_cache(cache)
        time.sleep(REQUEST_PAUSE_SECONDS)

    # Drop playlists that no longer exist/aren't followed, so the cache doesn't grow forever.
    for stale_id in set(playlists_cache) - seen_playlist_ids:
        del playlists_cache[stale_id]

    return all_ids


def _gather_library_albums(sp: spotipy.Spotify, cache: dict, progress_cb=None) -> set[str]:
    """Return the id of every album currently reachable from saved albums + liked songs + playlists."""
    progress_cb = progress_cb or (lambda msg: print(msg, flush=True))
    saved_ids = _gather_saved_albums(sp, cache, progress_cb)
    _save_cache(cache)
    liked_ids = _gather_liked_song_albums(sp, cache, progress_cb)
    _save_cache(cache)
    playlist_ids = _gather_playlist_albums(sp, cache, progress_cb)
    return saved_ids | liked_ids | playlist_ids


def _track_key(track: dict) -> tuple:
    """Identity key for matching the same song across two separate album_tracks calls.

    Spotify sometimes swaps in a new track ID for the same song (explicit-version
    or copyright re-uploads, common on hip-hop albums) independent of market
    availability. Matching by raw track ID then falsely flags the old ID as
    "region-locked" even though the song plays fine under its new ID. Disc +
    track number + sanitized name is stable across those re-uploads.
    """
    name = sanitize_filename(track.get("name", "")).strip().lower()
    return (track.get("disc_number", 1), track.get("track_number"), name)


def _chunked(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _fetch_album_tracks_batch(sp: spotipy.Spotify, album_ids: list[str], market: str = None) -> dict:
    """Return {album_id: [track_items]} for up to ALBUMS_BATCH_SIZE albums via one API call.

    Falls back to per-album pagination only for the rare album whose full
    tracklist doesn't fit on the embedded first page (Get Several Albums
    returns each album's tracks as a normal paging object).
    """
    response = sp.albums(album_ids, market=market)
    tracks_by_album = {}
    for album in response["albums"]:
        if not album:
            continue
        page = album["tracks"]
        items = list(page["items"])
        while page.get("next"):
            page = sp.next(page)
            items.extend(page["items"])
        tracks_by_album[album["id"]] = items
    return tracks_by_album


def _locked_keys_for_batch(sp: spotipy.Spotify, album_ids: list[str], market: str) -> dict:
    """Return {album_id: (all_track_items, locked_track_keys)} for a batch of album ids.

    Fetches the market-scoped listing directly (so Track Relinking already
    resolves any swapped-in regional track) and diffs it against the
    unrestricted listing by _track_key() rather than by ID - see _track_key()
    for why raw-ID comparison produces false positives.
    """
    unrestricted = _fetch_album_tracks_batch(sp, album_ids)
    time.sleep(REQUEST_PAUSE_SECONDS)
    restricted = _fetch_album_tracks_batch(sp, album_ids, market=market)

    result = {}
    for album_id in album_ids:
        all_items = unrestricted.get(album_id, [])
        market_items = restricted.get(album_id, [])
        playable_keys = {_track_key(t) for t in market_items if t.get("is_playable", True)}
        locked_keys = {_track_key(t) for t in all_items} - playable_keys
        result[album_id] = (all_items, locked_keys)
    return result


def _empty_cache() -> dict:
    return {
        "saved_albums": {"newest_seen_id": None, "album_ids": []},
        "liked_songs": {
            "newest_seen_track_id": None,
            "album_ids": [],
            "in_progress_offset": 0,  # resume point for an interrupted walk, separate from newest_seen_track_id
            "in_progress_top_track_id": None,  # the top-of-list track id when the in-progress walk began
        },
        "playlists": {},  # playlist_id -> {"snapshot_id": ..., "album_ids": [...]}
        "albums_meta": {},  # album_id -> {"artist": ..., "album": ...}
        "album_lock_status": {},  # album_id -> {"checked_at": "YYYY-MM-DD", "locked_tracks": [...]}
    }


def _load_cache(path: str = CACHE_PATH) -> dict:
    cache = _empty_cache()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        cache.update({key: value for key, value in data.items() if key in cache})
    return cache


def _save_cache(cache: dict, path: str = CACHE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def _is_lock_status_stale(entry: dict) -> bool:
    checked_at = date.fromisoformat(entry["checked_at"])
    return date.today() - checked_at > timedelta(days=LOCK_STATUS_TTL_DAYS)


def _all_cached_album_ids(cache: dict) -> set[str]:
    """Union of every album id already recorded in the cache (from any previous run)."""
    ids = set(cache["saved_albums"]["album_ids"])
    ids.update(cache["liked_songs"]["album_ids"])
    for entry in cache["playlists"].values():
        ids.update(entry["album_ids"])
    return ids


def _build_sync_queue(cache: dict, album_ids) -> list[dict]:
    """Derive the sync queue fresh from cached lock-status + current local files.

    Doing this as a pure derive step (rather than accumulating it during the
    scan) means it works identically whether an album's lock-status came
    from this run or a previous one, and always reflects what's *currently*
    on disk even if nothing needed a fresh Spotify lookup this time.
    """
    sync_queue = []
    for album_id in album_ids:
        entry = cache["album_lock_status"].get(album_id)
        if not entry or not entry["locked_tracks"]:
            continue
        meta = cache["albums_meta"].get(album_id, {"artist": "Unknown", "album": "Unknown"})
        artist, album_name = meta["artist"], meta["album"]
        dest_folder = os.path.join(BASE_MUSIC_PATH, sanitize_filename(f"{artist} - {album_name}"))
        missing_titles = [
            track["name"]
            for track in entry["locked_tracks"]
            if not os.path.exists(
                os.path.join(dest_folder, f"{track['track_number']:02d} {sanitize_filename(track['name'])}.mp3")
            )
        ]
        if missing_titles:
            sync_queue.append({"artist": artist, "album": album_name, "missing_titles": missing_titles})
    return sync_queue


def scan_region_locked_albums(sp: spotipy.Spotify, market: str = DEFAULT_MARKET) -> list[dict]:
    """Scan saved albums + liked songs + playlists for tracks unavailable in `market` that aren't downloaded yet.

    Progress is saved to CACHE_PATH as it goes (see module docstring), so if
    this gets interrupted (rate limit, 5xx storm, Ctrl-C, crash), re-running
    it resumes for free instead of re-scanning everything - and a routine
    re-run of an already-scanned, mostly-unchanged library costs very little.

    Returns a sync queue: a list of
        {"artist": str, "album": str, "missing_titles": list[str]}
    one entry per album that has at least one region-locked track missing
    from BASE_MUSIC_PATH. Each entry unpacks directly into download_album():
        download_album(entry["artist"], entry["album"])
    """
    cache = _load_cache()
    lock_status = cache["album_lock_status"]
    # Seeded from whatever's already cached, so a gather-phase failure still
    # lets us return a sync queue built from prior runs' results instead of
    # an empty one - it gets overwritten with the freshly gathered set below
    # if gathering completes successfully.
    all_album_ids = _all_cached_album_ids(cache)

    try:
        all_album_ids = _gather_library_albums(sp, cache)
        _save_cache(cache)

        pending_ids = [
            album_id
            for album_id in all_album_ids
            if album_id not in lock_status or _is_lock_status_stale(lock_status[album_id])
        ]
        reused = len(all_album_ids) - len(pending_ids)
        print(
            f"[Info] {len(pending_ids)} album(s) need a fresh lock-status check "
            f"(of {len(all_album_ids)} total, {reused} reused from cache).",
            flush=True,
        )

        checked = reused
        for batch_ids in _chunked(pending_ids, ALBUMS_BATCH_SIZE):
            locked_by_album = _locked_keys_for_batch(sp, batch_ids, market)

            for album_id in batch_ids:
                meta = cache["albums_meta"].get(album_id, {"artist": "Unknown", "album": "Unknown"})
                checked += 1
                print(
                    f"[Scan] ({checked}/{len(all_album_ids)}) Checked '{meta['artist']} - {meta['album']}'...",
                    flush=True,
                )

                all_items, locked_keys = locked_by_album[album_id]
                locked_tracks = [
                    {
                        "track_number": track["track_number"],
                        "name": track["name"],
                    }
                    for track in all_items
                    if _track_key(track) in locked_keys
                ]
                lock_status[album_id] = {"checked_at": date.today().isoformat(), "locked_tracks": locked_tracks}

            _save_cache(cache)
            time.sleep(REQUEST_PAUSE_SECONDS)

        print(f"[Info] Finished checking {len(all_album_ids)} unique album(s).", flush=True)

    except SpotifyException as exc:
        if exc.http_status not in _RECOVERABLE_HTTP_STATUSES:
            raise
        retry_after = (exc.headers or {}).get("Retry-After", "unknown") if exc.http_status == 429 else "n/a"
        print(f"\n[Error] Spotify error {exc.http_status} hit (Retry-After: {retry_after}).")
        print("[Info] Progress saved to cache - re-run this script later; it will resume automatically.")
        _save_cache(cache)

    return _build_sync_queue(cache, all_album_ids)


def write_sync_queue(sync_queue: list[dict], path: str = SYNC_QUEUE_PATH) -> None:
    """Save the sync queue to a JSON file so it can be inspected/edited before downloading."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sync_queue, f, indent=2, ensure_ascii=False)


def sync_missing_region_locked_tracks(market: str = DEFAULT_MARKET, auto_download: bool = False) -> list[dict]:
    """Entry point: scan, print + save the sync queue, and optionally download it."""
    sp = get_user_spotify_client()

    print(f"[Info] Scanning saved albums, liked songs, and playlists for tracks unavailable in '{market}'...", flush=True)
    sync_queue = scan_region_locked_albums(sp, market)

    if not sync_queue:
        print("[Info] Nothing to sync - no missing region-locked tracks found.")
        return sync_queue

    print(f"\n[Info] Sync queue ({len(sync_queue)} album(s)):")
    for entry in sync_queue:
        titles = ", ".join(entry["missing_titles"])
        print(f"  - {entry['artist']} - {entry['album']}  (missing: {titles})")

    write_sync_queue(sync_queue)
    print(f"\n[Info] Saved sync queue to '{SYNC_QUEUE_PATH}'. Inspect/edit it, then run:")
    print(f"           python album_downloader.py {SYNC_QUEUE_PATH}")

    if auto_download:
        for entry in sync_queue:
            print(f"\n[Info] Downloading '{entry['album']}' by '{entry['artist']}'...")
            try:
                download_album(entry["artist"], entry["album"])
            except (AlbumDownloadError, SpotifyLookupError) as exc:
                print(f"[Error] {exc}")
                continue

    return sync_queue


if __name__ == "__main__":
    sync_missing_region_locked_tracks(auto_download="--download" in sys.argv)
