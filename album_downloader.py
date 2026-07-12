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


def _describe_spotify_exception(exc: SpotifyException) -> str:
    """Turn a raw SpotifyException into a short, user-facing message."""
    if exc.http_status == 429:
        retry_after = (exc.headers or {}).get("Retry-After")
        if retry_after:
            return f"Spotify rate limit reached; try again in {retry_after}s."
        return "Spotify rate limit reached; try again later."
    return f"Spotify API error ({exc.http_status}): {exc.msg}"


def _default_progress_callback(event: str, **data) -> None:
    """Reproduces the script's original console output; callers (e.g. a web
    backend) can pass their own callback to track progress structurally instead."""
    if event == "fetching_metadata":
        print(f"[Info] Fetching metadata for '{data['album']}' by '{data['artist']}'...")
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


def sanitize_filename(name: str) -> str:
    """Strip/replace characters that are illegal in Windows file paths."""
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[:*?"<>|]', "", name)
    return name.strip()


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


def fetch_album_metadata(sp: spotipy.Spotify, artist_name: str, album_name: str):
    """Look up the album on Spotify and return (artist, album, cover_url, tracklist)."""
    query = f"album:{album_name} artist:{artist_name}"
    try:
        results = sp.search(q=query, type="album", limit=1, market="US")
    except SpotifyException as exc:
        raise SpotifyLookupError(_describe_spotify_exception(exc)) from exc
    items = results.get("albums", {}).get("items", [])

    if not items:
        raise SpotifyLookupError(f"Could not find album '{album_name}' by '{artist_name}' on Spotify.")

    album = items[0]
    album_id = album["id"]
    real_artist = album["artists"][0]["name"]
    real_album_name = album["name"]
    cover_url = album["images"][0]["url"] if album["images"] else None

    # Collect all tracks, following pagination in case of long albums.
    try:
        tracks_page = sp.album_tracks(album_id, market="US")
        raw_tracks = list(tracks_page["items"])
        while tracks_page["next"]:
            tracks_page = sp.next(tracks_page)
            raw_tracks.extend(tracks_page["items"])
    except SpotifyException as exc:
        raise SpotifyLookupError(_describe_spotify_exception(exc)) from exc

    total_tracks = len(raw_tracks)
    tracklist = [
        {
            "track_number": track["track_number"],
            "title": track["name"],
            "total_tracks": total_tracks,
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
    response = requests.get(cover_url, timeout=15)
    response.raise_for_status()

    with open(cover_path, "wb") as f:
        f.write(response.content)

    print(f"[Success] Downloaded album cover -> {cover_path}")
    return cover_path


def download_track_audio(artist: str, title: str, file_path: str) -> None:
    """Search YouTube via yt-dlp and download/convert the best match to mp3 at file_path."""
    search_query = f"ytsearch1:{artist} - {title} (Audio)"
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
        ydl.download([search_query])


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
        sp, artist_name, album_name
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

        progress_callback("track_start", track_number=track_num, total=total, title=title)
        try:
            download_track_audio(real_artist, title, file_path)
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
