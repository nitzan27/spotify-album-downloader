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
from spotipy.oauth2 import SpotifyClientCredentials

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
BASE_MUSIC_PATH = r"C:\Users\nitza\Music\spotify songs"

# Spotify API credentials (free, no user login required - see module docstring).
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")


def sanitize_filename(name: str) -> str:
    """Strip/replace characters that are illegal in Windows file paths."""
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r'[:*?"<>|]', "", name)
    return name.strip()


def get_spotify_client() -> spotipy.Spotify:
    """Authenticate with Spotify using the Client Credentials flow (no user login)."""
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        print("[Error] Missing Spotify API credentials.")
        print("        Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.")
        print("        Get free credentials at https://developer.spotify.com/dashboard")
        sys.exit(1)

    auth_manager = SpotifyClientCredentials(
        client_id=SPOTIFY_CLIENT_ID, client_secret=SPOTIFY_CLIENT_SECRET
    )
    return spotipy.Spotify(auth_manager=auth_manager)


def fetch_album_metadata(sp: spotipy.Spotify, artist_name: str, album_name: str):
    """Look up the album on Spotify and return (artist, album, cover_url, tracklist)."""
    query = f"album:{album_name} artist:{artist_name}"
    results = sp.search(q=query, type="album", limit=1, market="US")
    items = results.get("albums", {}).get("items", [])

    if not items:
        print(f"[Error] Could not find album '{album_name}' by '{artist_name}' on Spotify.")
        sys.exit(1)

    album = items[0]
    album_id = album["id"]
    real_artist = album["artists"][0]["name"]
    real_album_name = album["name"]
    cover_url = album["images"][0]["url"] if album["images"] else None

    # Collect all tracks, following pagination in case of long albums.
    tracks_page = sp.album_tracks(album_id, market="US")
    raw_tracks = list(tracks_page["items"])
    while tracks_page["next"]:
        tracks_page = sp.next(tracks_page)
        raw_tracks.extend(tracks_page["items"])

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


def download_album(artist_name: str, album_name: str) -> None:
    """Main pipeline: fetch metadata, download tracks, tag them, all in one album folder."""
    sp = get_spotify_client()

    print(f"[Info] Fetching metadata for '{album_name}' by '{artist_name}'...")
    real_artist, real_album_name, cover_url, tracklist = fetch_album_metadata(
        sp, artist_name, album_name
    )

    folder_name = sanitize_filename(f"{real_artist} - {real_album_name}")
    dest_folder = os.path.join(BASE_MUSIC_PATH, folder_name)
    os.makedirs(dest_folder, exist_ok=True)
    print(f"[Info] Output folder: {dest_folder}")

    cover_path = download_cover_image(cover_url, dest_folder)

    total = len(tracklist)
    for track in tracklist:
        track_num = track["track_number"]
        title = track["title"]
        safe_title = sanitize_filename(title)
        filename = f"{track_num:02d} {safe_title}.mp3"
        file_path = os.path.join(dest_folder, filename)

        print(f"[{track_num}/{total}] Downloading: {title}...")
        try:
            download_track_audio(real_artist, title, file_path)
        except Exception as exc:
            print(f"[Error] Failed to download '{title}': {exc}")
            continue

        if not os.path.exists(file_path):
            print(f"[Error] Expected file not found after download: {file_path}")
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
            print(f"[Success] Tagged {filename}")
        except Exception as exc:
            print(f"[Error] Failed to tag '{filename}': {exc}")

    print(f"\n[Done] '{real_album_name}' by '{real_artist}' saved to:\n{dest_folder}")


def download_from_queue_file(queue_path: str) -> None:
    """Read a JSON sync queue (as produced by sync_missing.py) and download every album in it."""
    with open(queue_path, "r", encoding="utf-8") as f:
        queue = json.load(f)

    print(f"[Info] Loaded {len(queue)} album(s) from '{queue_path}'.")
    for entry in queue:
        print(f"\n[Info] Downloading '{entry['album']}' by '{entry['artist']}'...")
        download_album(entry["artist"], entry["album"])


if __name__ == "__main__":
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
