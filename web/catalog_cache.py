"""Shared, cross-user cache of Spotify *catalog* facts - not a per-user cache.

Whether album X has region-locked tracks in market Y is a fact about
Spotify's catalog, true for whoever asks - it doesn't depend on which friend
is scanning. So unlike the per-user scan cache in scan_adapter.py (saved
albums/liked songs/playlists - private "what does this friend have" data,
one file per Spotify user id), the region-lock result for an album is
looked up here once, by whichever friend's scan needs it first, and every
other friend's scan reuses it - cutting the most expensive part of a scan
(2 Spotify API calls per batch of 20 albums) across the whole friend group
instead of paying it once per person.

Keyed by (album_id, market), never just album_id, since lock status is
market-specific and different friends can pick different markets in the
same scan form.

SQLite (stdlib, no new dependency) on the same ephemeral Render disk the
per-user cache files already live on: it survives normal operation (every
friend's scan benefits from every other friend's prior scans for as long as
the instance stays warm) but resets on a spin-down/redeploy, same
limitation as everything else under web/ - see web/CLAUDE.md's "Known
limitations". Not attempting anything fancier (a real hosted DB) given the
scale of a friends-only hobby project.
"""

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, timedelta

from sync_missing import LOCK_STATUS_TTL_DAYS

DB_PATH = os.path.join(os.path.dirname(__file__), ".web_scan_cache", "catalog.db")


def _connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lock_status (
            album_id TEXT NOT NULL,
            market TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            locked_tracks TEXT NOT NULL,
            PRIMARY KEY (album_id, market)
        )
        """
    )
    return conn


def get_lock_statuses(album_ids, market: str) -> dict:
    """Return {album_id: [locked_track, ...]} for whichever of `album_ids` have
    a fresh (checked within LOCK_STATUS_TTL_DAYS) cached result for `market`."""
    album_ids = list(album_ids)
    if not album_ids:
        return {}
    cutoff = (date.today() - timedelta(days=LOCK_STATUS_TTL_DAYS)).isoformat()
    placeholders = ",".join("?" * len(album_ids))
    # `with conn:` only wraps the transaction (commit/rollback) - it does NOT
    # close the connection, so `closing()` is needed too or every call leaks
    # a file handle (caught by a Windows PermissionError during cleanup in
    # testing - sqlite's connections aren't auto-closed like most DB-API
    # context managers).
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            f"SELECT album_id, locked_tracks FROM lock_status "
            f"WHERE market = ? AND checked_at >= ? AND album_id IN ({placeholders})",
            [market, cutoff, *album_ids],
        ).fetchall()
    return {album_id: json.loads(locked_tracks_json) for album_id, locked_tracks_json in rows}


def set_lock_statuses(entries: dict, market: str) -> None:
    """entries: {album_id: [locked_track, ...]} - upserts today's date as checked_at."""
    if not entries:
        return
    today = date.today().isoformat()
    with closing(_connect()) as conn, conn:
        conn.executemany(
            """
            INSERT INTO lock_status (album_id, market, checked_at, locked_tracks)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(album_id, market) DO UPDATE SET
                checked_at = excluded.checked_at,
                locked_tracks = excluded.locked_tracks
            """,
            [(album_id, market, today, json.dumps(tracks)) for album_id, tracks in entries.items()],
        )
