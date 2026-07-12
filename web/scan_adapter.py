"""Adapts sync_missing.py's region-lock scan to run as a web Job.

Reuses sync_missing.py's cache-aware, sp-parameterized scan machinery
directly (pure functions, safe across concurrent users' sp clients - see
CLAUDE.md). Two separate caches are involved, split by whether the data is
private to one friend or a shared fact about Spotify's catalog:
  - Each friend's own cache (saved albums, liked songs, playlists - "what
    does this friend have") lives in its own file under WEB_CACHE_DIR, keyed
    by Spotify user id (stable across logins) - never sync_missing.py's
    single local CACHE_PATH - so two friends' library contents never mix.
  - Region-lock status is NOT private - whether album X is locked in market
    Y is true for whoever asks, so it's looked up via web/catalog_cache.py's
    shared SQLite store instead of the per-user file's album_lock_status
    field: once any friend's scan checks an album+market, every other
    friend's scan with an overlapping album reuses that result instead of
    re-paying the 2-API-call lock-check cost.

Progress is written to job.progress (not printed), and the local-library
"already downloaded" check is dropped entirely (the server has no
visibility into any friend's local files - every region-locked album found
is shown, and the friend skips ones they already have).

Because the per-user cache file is durable and updated incrementally by the
same sync_missing.py helpers the CLI uses (each playlist, then each
lock-check batch, saved as it's processed), it doubles as both the
across-scan cache AND the resume mechanism for an interrupted scan - there's
no separate in-memory checkpoint to build or pass around. A rate_limited job
auto-requeues itself (web/jobs.py's MAX_SCAN_AUTO_RETRIES) or can be resumed
manually via the "Resume scan" button; either way it's just re-running
process_scan_job for the same session, and both caches already have
everything saved from the previous attempt.
"""

import os
import time

from spotipy.exceptions import SpotifyException

from sync_missing import (
    ALBUMS_BATCH_SIZE,
    REQUEST_PAUSE_SECONDS,
    _all_cached_album_ids,
    _chunked,
    _gather_library_albums,
    _load_cache,
    _locked_keys_for_batch,
    _RECOVERABLE_HTTP_STATUSES,
    _save_cache,
    _track_key,
)
from web import catalog_cache
from web.jobs import Job, create_scan_job
from web.sessions import Session, get_session
from web.spotify_auth import get_authenticated_client

WEB_CACHE_DIR = os.path.join(os.path.dirname(__file__), ".web_scan_cache")


def _cache_path_for(user_id: str) -> str:
    os.makedirs(WEB_CACHE_DIR, exist_ok=True)
    return os.path.join(WEB_CACHE_DIR, f"{user_id}.json")


def _get_spotify_user_id(session: Session, sp) -> str:
    """Fetch+cache the Spotify user id once per session (web/app.py's /me route
    usually already populated this; this is just a defensive fallback)."""
    if not session.spotify_user_id:
        session.spotify_user_id = sp.current_user()["id"]
    return session.spotify_user_id


def _wrap_call_counter(sp) -> dict:
    """Monkeypatch this sp instance to count every real Spotify HTTP request it
    makes. Every spotipy public method (playlist_items, albums, next, ...)
    funnels through _internal_call for the actual request, so patching it
    here counts calls regardless of which higher-level helper triggered them.

    Backend-only visibility: printed to the server console at the end of
    process_scan_job, never surfaced to the friend via job.progress/results.
    """
    counter = {"count": 0}
    original_internal_call = sp._internal_call

    def counting_internal_call(*args, **kwargs):
        counter["count"] += 1
        return original_internal_call(*args, **kwargs)

    sp._internal_call = counting_internal_call
    return counter


def _build_sync_queue(cache: dict, locked_tracks_by_album: dict, album_ids) -> list[dict]:
    """Like sync_missing._build_sync_queue, minus the local-file check - the
    server can't see any friend's local library, so every locked track found
    is surfaced and the friend decides what they already have."""
    sync_queue = []
    for album_id in album_ids:
        locked_tracks = locked_tracks_by_album.get(album_id)
        if not locked_tracks:
            continue
        meta = cache["albums_meta"].get(album_id, {"artist": "Unknown", "album": "Unknown"})
        missing_titles = [track["name"] for track in locked_tracks]
        sync_queue.append({"artist": meta["artist"], "album": meta["album"], "missing_titles": missing_titles})
    return sync_queue


def _mark_rate_limited(job: Job, exc: SpotifyException, stage: str, cache: dict, cache_path: str) -> None:
    _save_cache(cache, cache_path)
    if exc.http_status == 429:
        retry_after = (exc.headers or {}).get("Retry-After")
        job.retry_after_seconds = float(retry_after) if retry_after and retry_after.isdigit() else None
        retry_after_display = retry_after or "unknown"
    else:
        job.retry_after_seconds = None
        retry_after_display = "n/a"
    job.progress = f"Rate limited by Spotify while {stage} (retry after {retry_after_display}s) - progress saved."
    job.status = "rate_limited"


def process_scan_job(job: Job) -> None:
    """Scan the job's session's Spotify library for region-locked tracks.

    On success, sets job.results to a sync-queue-shaped list. On a
    429/500/502/503/504 (a 502 storm exhausting spotipy's own retries also
    surfaces as a 429 - see web/spotify_auth.py's status_retries=0 fix),
    sets job.status = "rate_limited" (jobs._run_worker_loop only
    auto-promotes a still-"running" job to "done", so this sticks, and
    either auto-requeues the same job or waits for a manual resume).
    """
    session = get_session(job.session_id)
    if session is None:
        raise RuntimeError("Login session expired; please log in again.")

    sp = get_authenticated_client(session)
    if sp is None:
        raise RuntimeError("Not logged in.")

    call_counter = _wrap_call_counter(sp)
    gather_call_count = 0

    try:
        user_id = _get_spotify_user_id(session, sp)
        cache_path = _cache_path_for(user_id)
        cache = _load_cache(cache_path)
        # Seeded from whatever's cached so a gather-phase failure still lets us
        # return prior-run results instead of nothing; overwritten below if
        # gathering completes successfully.
        all_album_ids = _all_cached_album_ids(cache)

        def progress_cb(msg: str) -> None:
            job.progress = msg

        try:
            all_album_ids = _gather_library_albums(sp, cache, progress_cb)
            _save_cache(cache, cache_path)
        except SpotifyException as exc:
            if exc.http_status not in _RECOVERABLE_HTTP_STATUSES:
                raise
            _mark_rate_limited(job, exc, "gathering your library", cache, cache_path)
            return
        finally:
            gather_call_count = call_counter["count"]

        # Region-lock status comes from the shared cross-user catalog cache, not
        # the per-user file - see module docstring. Any album another friend has
        # already checked for this market is reused here at zero API cost.
        locked_tracks_by_album = catalog_cache.get_lock_statuses(all_album_ids, job.market)
        pending_ids = [album_id for album_id in all_album_ids if album_id not in locked_tracks_by_album]
        total = len(all_album_ids)
        checked = total - len(pending_ids)

        try:
            for batch_ids in _chunked(pending_ids, ALBUMS_BATCH_SIZE):
                locked_by_album = _locked_keys_for_batch(sp, batch_ids, job.market)

                fresh_entries = {}
                for album_id in batch_ids:
                    meta = cache["albums_meta"].get(album_id, {"artist": "Unknown", "album": "Unknown"})
                    checked += 1
                    job.progress = f"Checking {checked}/{total}: {meta['artist']} - {meta['album']}"

                    all_items, locked_keys = locked_by_album[album_id]
                    locked_tracks = [
                        {"track_number": track["track_number"], "name": track["name"]}
                        for track in all_items
                        if _track_key(track) in locked_keys
                    ]
                    locked_tracks_by_album[album_id] = locked_tracks
                    fresh_entries[album_id] = locked_tracks

                catalog_cache.set_lock_statuses(fresh_entries, job.market)
                _save_cache(cache, cache_path)
                time.sleep(REQUEST_PAUSE_SECONDS)

        except SpotifyException as exc:
            if exc.http_status not in _RECOVERABLE_HTTP_STATUSES:
                raise
            _mark_rate_limited(job, exc, "checking albums", cache, cache_path)
            return

        job.results = _build_sync_queue(cache, locked_tracks_by_album, all_album_ids)
        job.progress = f"Scan complete - {total} album(s) checked, {len(job.results)} with missing tracks."

    finally:
        # Backend-only diagnostic - never sent to the frontend. Prints on
        # every exit path (success, rate_limited, or an uncaught crash), so
        # even a failed scan tells you how much Spotify API budget it burned.
        total_calls = call_counter["count"]
        print(
            f"[scan_adapter] job {job.id} (session={job.session_id}, market={job.market}): "
            f"{total_calls} Spotify API call(s) total "
            f"(gather: {gather_call_count}, lock-check: {total_calls - gather_call_count})",
            flush=True,
        )


def resume_scan_job(old_job: Job) -> Job:
    """Start a fresh scan Job for the same session/market.

    No checkpoint needs to be handed over - the per-user cache file on disk
    already has everything saved from the previous attempt, so the new job
    picks up where the old one left off automatically.
    """
    return create_scan_job(old_job.session_id, old_job.market)
