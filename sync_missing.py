"""
Region-Lock Sync
=================

Scans your saved albums and playlists for tracks Spotify won't let you play
in your market, checks whether you already have them downloaded locally, and
queues up the rest for `download_album()` to fetch from YouTube instead.

Why a separate user login
--------------------------
album_downloader.py authenticates with the Client Credentials flow, which is
app-only and has no access to anything under `/me/*` (saved albums,
playlists). Reading a user's library requires the Authorization Code flow
with a real login + scopes, so this module opens its own browser-based login
the first time it runs and caches the resulting token in `.spotify_user_cache`.

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

import itertools
import json
import os
import sys
import time

import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth

from album_downloader import (
    BASE_MUSIC_PATH,
    SPOTIFY_CLIENT_ID,
    SPOTIFY_CLIENT_SECRET,
    download_album,
    sanitize_filename,
)

DEFAULT_MARKET = "IL"
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8080/callback")
USER_SCOPE = "user-library-read playlist-read-private playlist-read-collaborative"
TOKEN_CACHE_PATH = ".spotify_user_cache"
SYNC_QUEUE_PATH = "sync_queue.json"
SCAN_STATE_PATH = ".sync_scan_state.json"

# Batch size for the "Get Several Albums" endpoint (its hard max is 20). Using
# it instead of one album_tracks() call per album cuts request volume ~20x,
# which is what was tripping Spotify's rate limiter on a 400+ album library.
ALBUMS_BATCH_SIZE = 20
REQUEST_PAUSE_SECONDS = 0.2


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
    return spotipy.Spotify(auth_manager=auth_manager, retries=0)


def _paginate(sp: spotipy.Spotify, first_page: dict):
    """Yield every item across all pages of a spotipy paging object."""
    page = first_page
    while page:
        for item in page["items"]:
            yield item
        page = sp.next(page) if page.get("next") else None


def _iter_saved_albums(sp: spotipy.Spotify):
    """Yield each album (full album object) from the user's saved albums."""
    first_page = sp.current_user_saved_albums(limit=50)
    print(f"[Info] Found {first_page.get('total', 0)} saved album(s) to check.", flush=True)
    for item in _paginate(sp, first_page):
        yield item["album"]


def _iter_playlist_albums(sp: spotipy.Spotify):
    """Yield each unique album referenced by a track in any of the user's playlists."""
    seen_album_ids = set()
    first_page = sp.current_user_playlists(limit=50)
    print(f"[Info] Found {first_page.get('total', 0)} playlist(s) to scan for albums.", flush=True)
    for playlist in _paginate(sp, first_page):
        print(f"[Info] Scanning playlist '{playlist['name']}'...", flush=True)
        track_page = sp.playlist_items(playlist["id"], additional_types=("track",), limit=100)
        for item in _paginate(sp, track_page):
            track = item.get("track")
            if not track or not track.get("album"):
                continue
            album = track["album"]
            if album["id"] in seen_album_ids:
                continue
            seen_album_ids.add(album["id"])
            yield album


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


def _load_scan_state(path: str = SCAN_STATE_PATH) -> dict:
    if not os.path.exists(path):
        return {"scanned_album_ids": [], "sync_queue": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_scan_state(state: dict, path: str = SCAN_STATE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def scan_region_locked_albums(sp: spotipy.Spotify, market: str = DEFAULT_MARKET) -> list[dict]:
    """Scan saved albums + playlists for tracks unavailable in `market` that aren't downloaded yet.

    Progress is checkpointed to SCAN_STATE_PATH after every batch, so if this
    gets interrupted (rate limit, Ctrl-C, crash), re-running it resumes from
    where it left off instead of re-scanning albums already checked.

    Returns a sync queue: a list of
        {"artist": str, "album": str, "missing_titles": list[str]}
    one entry per album that has at least one region-locked track missing
    from BASE_MUSIC_PATH. Each entry unpacks directly into download_album():
        download_album(entry["artist"], entry["album"])
    """
    state = _load_scan_state()
    scanned_ids = set(state["scanned_album_ids"])
    sync_queue = state["sync_queue"]

    if scanned_ids:
        print(f"[Info] Resuming previous scan - {len(scanned_ids)} album(s) already checked.", flush=True)

    try:
        albums_by_id = {}
        for album in itertools.chain(_iter_saved_albums(sp), _iter_playlist_albums(sp)):
            albums_by_id.setdefault(album["id"], album)

        pending_ids = [album_id for album_id in albums_by_id if album_id not in scanned_ids]
        print(f"[Info] {len(pending_ids)} album(s) left to check (of {len(albums_by_id)} total).", flush=True)

        checked = len(scanned_ids)
        for batch_ids in _chunked(pending_ids, ALBUMS_BATCH_SIZE):
            locked_by_album = _locked_keys_for_batch(sp, batch_ids, market)

            for album_id in batch_ids:
                album = albums_by_id[album_id]
                artist = album["artists"][0]["name"]
                album_name = album["name"]
                checked += 1
                print(f"[Scan] ({checked}/{len(albums_by_id)}) Checking '{artist} - {album_name}'...", flush=True)

                all_items, locked_keys = locked_by_album[album_id]
                scanned_ids.add(album_id)
                if not locked_keys:
                    continue

                dest_folder = os.path.join(BASE_MUSIC_PATH, sanitize_filename(f"{artist} - {album_name}"))
                missing_titles = [
                    track["name"]
                    for track in all_items
                    if _track_key(track) in locked_keys
                    and not os.path.exists(
                        os.path.join(
                            dest_folder,
                            f"{track['track_number']:02d} {sanitize_filename(track['name'])}.mp3",
                        )
                    )
                ]

                if missing_titles:
                    print(f"[Scan]     -> {len(missing_titles)} missing track(s), added to sync queue.", flush=True)
                    sync_queue.append({"artist": artist, "album": album_name, "missing_titles": missing_titles})

            _save_scan_state({"scanned_album_ids": sorted(scanned_ids), "sync_queue": sync_queue})
            time.sleep(REQUEST_PAUSE_SECONDS)

        print(f"[Info] Finished scanning {checked} unique album(s).", flush=True)
        if os.path.exists(SCAN_STATE_PATH):
            os.remove(SCAN_STATE_PATH)

    except SpotifyException as exc:
        if exc.http_status != 429:
            raise
        retry_after = (exc.headers or {}).get("Retry-After", "unknown")
        print(f"\n[Error] Spotify rate limit hit (Retry-After: {retry_after}s).")
        print(f"[Info] Progress saved - {len(scanned_ids)} album(s) checked so far.")
        print("[Info] Re-run this script later; it will resume automatically.")
        _save_scan_state({"scanned_album_ids": sorted(scanned_ids), "sync_queue": sync_queue})

    return sync_queue


def write_sync_queue(sync_queue: list[dict], path: str = SYNC_QUEUE_PATH) -> None:
    """Save the sync queue to a JSON file so it can be inspected/edited before downloading."""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sync_queue, f, indent=2, ensure_ascii=False)


def sync_missing_region_locked_tracks(market: str = DEFAULT_MARKET, auto_download: bool = False) -> list[dict]:
    """Entry point: scan, print + save the sync queue, and optionally download it."""
    sp = get_user_spotify_client()

    print(f"[Info] Scanning saved albums and playlists for tracks unavailable in '{market}'...", flush=True)
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
            download_album(entry["artist"], entry["album"])

    return sync_queue


if __name__ == "__main__":
    sync_missing_region_locked_tracks(auto_download="--download" in sys.argv)
