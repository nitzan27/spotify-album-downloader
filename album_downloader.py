"""
Spotify Album Downloader & Tagger
==================================

Given an artist name and an album name, this script:
  1. Fetches the official tracklist + album art from the Spotify Web API (via spotipy).
  2. Downloads each track's audio from YouTube (via yt-dlp), converted to .mp3.
  3. Embeds full ID3 metadata (title, artist, album, track number, cover art) with mutagen.
  4. Saves everything into "<BASE_MUSIC_PATH>\\<Artist> - <Album>\\NN Title.mp3".

Setup
-----
1. Install dependencies:
       pip install spotipy yt-dlp mutagen requests
2. Install ffmpeg and make sure it's on your system PATH (required by yt-dlp to
   convert downloaded audio to .mp3).
3. Get free Spotify API credentials at https://developer.spotify.com/dashboard
   (create an app, no user login/OAuth needed - just the Client ID/Secret), then
   set them as environment variables:
       setx SPOTIFY_CLIENT_ID "your_client_id"
       setx SPOTIFY_CLIENT_SECRET "your_client_secret"
   (or hardcode them in the CONFIGURATION section below).
"""

import json
import os
import re
import sys
import time
from typing import Optional

import requests
import spotipy
import yt_dlp
from dotenv import load_dotenv
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TRCK, APIC
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyClientCredentials

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
BASE_MUSIC_PATH = r"C:\Users\nitza\Music\spotify songs"

# Spotify API credentials (free, no user login required - see module docstring).
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")


class SpotifyLookupError(Exception):
    """Raised for user-facing Spotify lookup failures (missing creds, no match, etc.).

    Raised instead of calling sys.exit() so this code can be reused from a
    non-main thread (e.g. a web backend's job worker) - sys.exit() there would
    silently kill only that thread instead of surfacing an error.
    """


class SpotifyRateLimitError(SpotifyLookupError):
    """A SpotifyLookupError specifically caused by a 429.

    Carries the Retry-After delay (seconds, if Spotify sent one) as a typed
    field so a caller that wants to auto-retry (e.g. the web app's job
    workers - see web/downloader_adapter.py) doesn't have to string-parse the
    human-readable message to find it. Still a plain SpotifyLookupError as
    far as the CLI's __main__ is concerned, so its existing broad
    `except SpotifyLookupError` catch needs no change.
    """

    def __init__(self, message: str, retry_after_seconds: Optional[float] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _spotify_lookup_error(exc: SpotifyException) -> SpotifyLookupError:
    """Turn a raw SpotifyException into a SpotifyLookupError, or a
    SpotifyRateLimitError (with the Retry-After delay, if any) for a 429."""
    if exc.http_status == 429:
        retry_after = (exc.headers or {}).get("Retry-After")
        if retry_after:
            try:
                retry_after_seconds = float(retry_after)
            except ValueError:
                retry_after_seconds = None
            return SpotifyRateLimitError(
                f"Spotify rate limit reached; try again in {retry_after}s.", retry_after_seconds
            )
        return SpotifyRateLimitError("Spotify rate limit reached; try again later.")
    return SpotifyLookupError(f"Spotify API error ({exc.http_status}): {exc.msg}")


def _default_progress_callback(event: str, **data) -> None:
    """Reproduces the script's original console output; callers (e.g. a web
    backend) can pass their own callback to track progress structurally instead."""
    if event == "fetching_metadata":
        print(f"[Info] Fetching metadata for '{data['album']}' by '{data['artist']}'...")
    elif event == "musicbrainz_fallback":
        print(f"[Info] '{data['album']}' not found on Spotify - trying MusicBrainz...")
    elif event == "output_folder":
        print(f"[Info] Output folder: {data['dest_folder']}")
    elif event == "track_start":
        print(f"[{data['track_number']}/{data['total']}] Downloading: {data['title']}...")
    elif event == "track_done":
        if data["success"]:
            print(f"[Success] Tagged {data['filename']}")
        else:
            print(f"[Error] {data['error']}")
    elif event == "done":
        print(f"\n[Done] '{data['album']}' by '{data['artist']}' saved to:\n{data['dest_folder']}")


_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """Strip/replace characters that are illegal in Windows file paths."""
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[:*?"<>|]', "", name)
    name = name.strip()
    # A trailing dot/space in a path segment is silently dropped by Windows
    # itself, so this was never visible from the CLI - but the web app's
    # File System Access API folder picker enforces the same Windows rule
    # explicitly and raises instead of stripping it, so it must be handled
    # here too (see downloadFolder.ts's sanitizeFilename(), which mirrors
    # this function).
    name = re.sub(r"[. ]+$", "", name)
    if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        name += "_"
    return name


def get_spotify_client() -> spotipy.Spotify:
    """Authenticate with Spotify using the Client Credentials flow (no user login)."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        raise SpotifyLookupError(
            "Missing Spotify API credentials. Set SPOTIFY_CLIENT_ID and "
            "SPOTIFY_CLIENT_SECRET environment variables. Get free credentials "
            "at https://developer.spotify.com/dashboard"
        )

    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
    )
    # retries=0: spotipy's default retry adapter sleeps for whatever Retry-After
    # a 429 response gives (can be many hours) before raising anything, which
    # would hang a caller (e.g. a web job's worker thread) instead of failing
    # fast. See fetch_album_metadata() for where the resulting 429 is turned
    # into a clean, user-facing error instead.
    # status_retries=0 too: retries=0 alone only zeroes urllib3's overall
    # `total` retry count - status-code retries (429/500/502/503/504) are a
    # separate counter that defaults to 3 regardless of `retries`, so a run
    # of 502s would still silently retry a few times with backoff first.
    return spotipy.Spotify(auth_manager=auth_manager, retries=0, status_retries=0)


_SEARCH_MARKET_FALLBACKS = ["US", "GB"]

MUSICBRAINZ_BASE = "https://musicbrainz.org/ws/2"
MUSICBRAINZ_USER_AGENT = "spotify-album-downloader/1.0 ( personal hobby project, no support contact )"
COVER_ART_ARCHIVE_BASE = "https://coverartarchive.org"


def _search_spotify_album(sp: spotipy.Spotify, artist_name: str, album_name: str):
    """Try the album search across a few markets, since Spotify's search
    endpoint's market filter doesn't always agree with true catalog-wide
    availability - e.g. My Bloody Valentine's "Loveless" returns 0 results
    for market="US"/"IL"/no-market but 1 for market="GB", even though the
    album's tracks are fully fetchable/playable under market="US" once its
    id is known. Returns (album, market) for the first market that finds a
    hit, or (None, None) if every fallback market comes back empty."""
    query = f"album:{album_name} artist:{artist_name}"
    for market in _SEARCH_MARKET_FALLBACKS:
        try:
            results = sp.search(q=query, type="album", limit=1, market=market)
        except SpotifyException as exc:
            raise _spotify_lookup_error(exc) from exc
        items = results.get("albums", {}).get("items", [])
        if items:
            return items[0], market
    return None, None


def _musicbrainz_lookup(artist_name: str, album_name: str) -> Optional[tuple]:
    """Fallback for albums genuinely absent from Spotify's catalog under any
    market - MusicBrainz is a free, crowd-sourced discography that covers
    releases never licensed to any streaming service at all. Returns the
    same (real_artist, real_album_name, cover_url, tracklist) shape
    fetch_album_metadata() returns from Spotify, so the rest of the pipeline
    doesn't need to know which source it came from - or None if MusicBrainz
    has no match either (never raises; this is a last-resort lookup)."""
    headers = {"User-Agent": MUSICBRAINZ_USER_AGENT}
    query = f'release:"{album_name}" AND artist:"{artist_name}"'
    try:
        search_resp = requests.get(
            f"{MUSICBRAINZ_BASE}/release/",
            params={"query": query, "fmt": "json", "limit": 1},
            headers=headers,
            timeout=15,
        )
        search_resp.raise_for_status()
        releases = search_resp.json().get("releases", [])
        if not releases:
            return None
        release_id = releases[0]["id"]

        # MusicBrainz's usage policy asks for roughly 1 request/second.
        time.sleep(1)

        detail_resp = requests.get(
            f"{MUSICBRAINZ_BASE}/release/{release_id}",
            params={"inc": "recordings", "fmt": "json"},
            headers=headers,
            timeout=15,
        )
        detail_resp.raise_for_status()
        detail = detail_resp.json()
    except (requests.RequestException, ValueError):
        return None

    raw_tracks = [track for medium in detail.get("media", []) for track in medium.get("tracks", [])]
    if not raw_tracks:
        return None

    artist_credit = detail.get("artist-credit") or []
    real_artist = artist_credit[0]["name"] if artist_credit else artist_name
    real_album_name = detail.get("title", album_name)

    total_tracks = len(raw_tracks)
    tracklist = [
        {
            "track_number": i,
            "title": track["title"],
            "total_tracks": total_tracks,
            "duration_ms": track.get("length"),
        }
        for i, track in enumerate(raw_tracks, start=1)
    ]

    # Not verified to exist (many releases have no art uploaded) - a 404 here
    # is expected and handled by download_cover_image()'s own fallback.
    cover_url = f"{COVER_ART_ARCHIVE_BASE}/release/{release_id}/front"

    return real_artist, real_album_name, cover_url, tracklist


def fetch_album_metadata(
    sp: spotipy.Spotify, artist_name: str, album_name: str, progress_callback=None
):
    """Look up the album and return (artist, album, cover_url, tracklist).

    Tries Spotify first (across _SEARCH_MARKET_FALLBACKS), then falls back to
    MusicBrainz + Cover Art Archive for albums genuinely absent from Spotify's
    catalog under any market - see _search_spotify_album()/_musicbrainz_lookup().
    """
    album, market = _search_spotify_album(sp, artist_name, album_name)

    if album is None:
        if progress_callback:
            progress_callback("musicbrainz_fallback", artist=artist_name, album=album_name)
        fallback = _musicbrainz_lookup(artist_name, album_name)
        if fallback is None:
            raise SpotifyLookupError(
                f"Could not find album '{album_name}' by '{artist_name}' on Spotify or MusicBrainz."
            )
        return fallback

    album_id = album["id"]
    real_artist = album["artists"][0]["name"]
    real_album_name = album["name"]
    cover_url = album["images"][0]["url"] if album["images"] else None

    # Collect all tracks, following pagination in case of long albums.
    try:
        tracks_page = sp.album_tracks(album_id, market=market)
        raw_tracks = list(tracks_page["items"])
        while tracks_page["next"]:
            tracks_page = sp.next(tracks_page)
            raw_tracks.extend(tracks_page["items"])
    except SpotifyException as exc:
        raise _spotify_lookup_error(exc) from exc

    total_tracks = len(raw_tracks)
    tracklist = [
        {
            "track_number": track["track_number"],
            "title": track["name"],
            "total_tracks": total_tracks,
            "duration_ms": track.get("duration_ms"),
        }
        for track in raw_tracks
    ]

    return real_artist, real_album_name, cover_url, tracklist


def download_cover_image(cover_url: str, dest_folder: str) -> str | None:
    """Download the album cover to '00 cover.jpg' inside dest_folder."""
    if not cover_url:
        print("[Warning] No album art available; skipping cover download.")
        return None

    cover_path = os.path.join(dest_folder, "00 cover.jpg")
    try:
        response = requests.get(cover_url, timeout=15)
        response.raise_for_status()
    except requests.RequestException:
        # Expected for the Cover Art Archive fallback URL - many releases
        # never had art uploaded there, unlike Spotify's own cover_url which
        # is always valid when present.
        print("[Warning] Could not download album art; skipping.")
        return None

    with open(cover_path, "wb") as f:
        f.write(response.content)

    print(f"[Success] Downloaded album cover -> {cover_path}")
    return cover_path


_SEARCH_CANDIDATES = 5
_MIN_DURATION_TOLERANCE_SECONDS = 15
_DURATION_TOLERANCE_RATIO = 0.12


class TrackNotFoundError(Exception):
    """Raised when no YouTube search result's duration plausibly matches the
    Spotify track - e.g. the track isn't really uploaded as a standalone song,
    so the top hit is something unrelated (a mix, full album, etc.)."""


def _pick_best_duration_match(entries: list[dict], expected_duration_sec: float) -> Optional[dict]:
    """Return the candidate entry whose duration is closest to expected_duration_sec,
    but only if it's within tolerance - otherwise None (no plausible match)."""
    tolerance = max(_MIN_DURATION_TOLERANCE_SECONDS, _DURATION_TOLERANCE_RATIO * expected_duration_sec)
    candidates = [e for e in entries if e.get("duration") is not None]
    if not candidates:
        return None
    best = min(candidates, key=lambda e: abs(e["duration"] - expected_duration_sec))
    if abs(best["duration"] - expected_duration_sec) <= tolerance:
        return best
    return None


def download_track_audio(
    artist: str, title: str, expected_duration_sec: Optional[float], file_path: str
) -> None:
    """Search YouTube via yt-dlp and download/convert the best match to mp3 at file_path.

    If expected_duration_sec is known (from Spotify's track metadata), the top
    handful of search results are checked against it first and the closest
    in-tolerance one is downloaded - rejecting the search entirely (raising
    TrackNotFoundError) rather than blindly downloading the #1 hit when
    nothing matches. Without this, a track that isn't really uploaded to
    YouTube as a standalone song can silently download something wildly
    unrelated (e.g. a multi-hour mix) as if it were the real track.
    """
    search_query = f"ytsearch{_SEARCH_CANDIDATES}:{artist} - {title} (Audio)"

    download_target = search_query
    if expected_duration_sec is not None:
        search_opts = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(search_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
        entries = (info or {}).get("entries") or []
        best = _pick_best_duration_match(entries, expected_duration_sec)
        if best is None:
            closest = min((e["duration"] for e in entries if e.get("duration") is not None), default=None)
            raise TrackNotFoundError(
                f"No matching-length YouTube result for '{title}' "
                f"(expected ~{expected_duration_sec:.0f}s, closest candidate was "
                f"{'n/a' if closest is None else f'{closest:.0f}s'})"
            )
        download_target = f"https://www.youtube.com/watch?v={best['id']}"

    # yt-dlp appends the real extension itself; strip our fixed ".mp3" for the template.
    output_template = file_path[:-len(".mp3")] + ".%(ext)s"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # The web client's extraction path triggers YouTube's "Sign in to
        # confirm you're not a bot" check far more readily from datacenter
        # IPs (e.g. cloud hosts) than from residential ones; the Android/iOS
        # client's API doesn't hit the same check as of this writing.
        "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}},
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([download_target])


def tag_mp3(
    file_path: str,
    title: str,
    artist: str,
    album: str,
    track_number: int,
    total_tracks: int,
    cover_path: str | None,
) -> None:
    """Embed ID3 metadata (and cover art) into the mp3 file, replacing any existing tags."""
    try:
        ID3(file_path).delete(file_path)
    except ID3NoHeaderError:
        pass

    audio = ID3()
    audio["TIT2"] = TIT2(encoding=3, text=title)
    audio["TPE1"] = TPE1(encoding=3, text=artist)
    audio["TALB"] = TALB(encoding=3, text=album)
    audio["TRCK"] = TRCK(encoding=3, text=f"{track_number}/{total_tracks}")

    if cover_path and os.path.exists(cover_path):
        with open(cover_path, "rb") as img:
            audio["APIC"] = APIC(
                encoding=3,
                mime="image/jpeg",
                type=3,  # 3 = front cover
                desc="Cover",
                data=img.read(),
            )

    audio.save(file_path, v2_version=3)


def download_album(
    artist_name: str,
    album_name: str,
    dest_root: str = BASE_MUSIC_PATH,
    progress_callback=_default_progress_callback,
) -> str:
    """Main pipeline: fetch metadata, download tracks, tag them, all in one album folder.

    Returns the destination folder path. `dest_root` lets callers (e.g. a web
    backend) redirect output to a per-job temp directory instead of the
    hardcoded BASE_MUSIC_PATH. `progress_callback` lets callers observe
    progress structurally instead of scraping stdout.
    """
    sp = get_spotify_client()

    progress_callback("fetching_metadata", artist=artist_name, album=album_name)
    real_artist, real_album_name, cover_url, tracklist = fetch_album_metadata(
        sp, artist_name, album_name, progress_callback=progress_callback
    )

    folder_name = sanitize_filename(f"{real_artist} - {real_album_name}")
    dest_folder = os.path.join(dest_root, folder_name)
    os.makedirs(dest_folder, exist_ok=True)
    progress_callback("output_folder", dest_folder=dest_folder)

    cover_path = download_cover_image(cover_url, dest_folder)

    total = len(tracklist)
    for track in tracklist:
        track_num = track["track_number"]
        title = track["title"]
        safe_title = sanitize_filename(title)
        filename = f"{track_num:02d} {safe_title}.mp3"
        file_path = os.path.join(dest_folder, filename)

        duration_ms = track.get("duration_ms")
        expected_duration_sec = duration_ms / 1000 if duration_ms else None

        progress_callback("track_start", track_number=track_num, total=total, title=title)
        try:
            download_track_audio(real_artist, title, expected_duration_sec, file_path)
        except Exception as exc:
            progress_callback(
                "track_done", track_number=track_num, title=title, filename=filename,
                success=False, error=f"Failed to download '{title}': {exc}",
            )
            continue

        if not os.path.exists(file_path):
            progress_callback(
                "track_done", track_number=track_num, title=title, filename=filename,
                success=False, error=f"Expected file not found after download: {file_path}",
            )
            continue

        try:
            tag_mp3(
                file_path,
                title,
                real_artist,
                real_album_name,
                track_num,
                track["total_tracks"],
                cover_path,
            )
            progress_callback(
                "track_done", track_number=track_num, title=title, filename=filename,
                success=True, error=None,
            )
        except Exception as exc:
            progress_callback(
                "track_done", track_number=track_num, title=title, filename=filename,
                success=False, error=f"Failed to tag '{filename}': {exc}",
            )

    progress_callback("done", artist=real_artist, album=real_album_name, dest_folder=dest_folder)
    return dest_folder


def download_from_queue_file(queue_path: str) -> None:
    """Read a JSON sync queue (as produced by sync_missing.py) and download every album in it."""
    with open(queue_path, "r", encoding="utf-8") as f:
        queue = json.load(f)

    print(f"[Info] Loaded {len(queue)} album(s) from '{queue_path}'.")
    for entry in queue:
        print(f"\n[Info] Downloading '{entry['album']}' by '{entry['artist']}'...")
        download_album(entry["artist"], entry["album"])


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1:
            # A queue file was passed - download everything in it, no prompts.
            download_from_queue_file(sys.argv[1])
        else:
            artist_input = input("Enter Artist Name: ").strip()
            album_input = input("Enter Album Name: ").strip()

            if not artist_input or not album_input:
                print("[Error] Artist and Album name cannot be empty.")
                sys.exit(1)

            download_album(artist_input, album_input)
    except SpotifyLookupError as exc:
        print(f"[Error] {exc}")
        sys.exit(1)
