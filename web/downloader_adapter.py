"""Bridges a web Job to album_downloader.download_album().

Runs in a worker thread (via jobs.worker_loop -> run_in_threadpool). Downloads
into a per-job temp directory (so two concurrent jobs, even for the same
album, can never collide), zips the result, and deletes the uncompressed
mp3s so only the zip is left on disk.
"""

import os
import shutil
import tempfile

from album_downloader import download_album, SpotifyRateLimitError
from web.jobs import Job

TEMP_ROOT = os.path.join(tempfile.gettempdir(), "spotify_album_downloader_jobs")
os.makedirs(TEMP_ROOT, exist_ok=True)


def _job_dir(job: Job) -> str:
    return os.path.join(TEMP_ROOT, job.id)


def _make_progress_callback(job: Job):
    def progress_callback(event: str, **data) -> None:
        if event == "fetching_metadata":
            job.progress = f"Looking up '{data['album']}' by '{data['artist']}' on Spotify..."
        elif event == "musicbrainz_fallback":
            job.progress = f"'{data['album']}' not found on Spotify - trying MusicBrainz..."
        elif event == "output_folder":
            job.progress = "Downloading cover art..."
        elif event == "track_start":
            job.progress = f"Downloading track {data['track_number']}/{data['total']}: {data['title']}"
        elif event == "track_done" and not data["success"]:
            job.progress = f"Warning: {data['error']}"
        elif event == "done":
            job.progress = "Packaging download..."

    return progress_callback


def process_job(job: Job) -> None:
    """Download the album for `job` into a per-job temp dir and zip it. Raises on failure.

    A Spotify rate limit (shared app-wide across every friend using this
    site - see web/CLAUDE.md) is the one failure mode handled specially: it
    sets job.status = "rate_limited" and returns instead of raising, which
    lets web/jobs.py's worker loop auto-requeue it the same way a rate_limited
    scan job already does, rather than failing the whole download permanently
    on what's usually a transient, minutes-long condition.

    AlbumDownloadError (every track failed on every source) is deliberately
    NOT caught here - it propagates up to web/jobs.py's worker loop, whose
    existing generic `except Exception` already does the right thing
    (job.status = "error", job.error = str(exc)) with no extra code needed.
    """
    job_dir = _job_dir(job)
    os.makedirs(job_dir, exist_ok=True)

    try:
        result = download_album(
            job.artist,
            job.album,
            dest_root=job_dir,
            progress_callback=_make_progress_callback(job),
        )
    except SpotifyRateLimitError as exc:
        job.retry_after_seconds = exc.retry_after_seconds
        job.progress = str(exc)
        job.status = "rate_limited"
        return

    zip_base = result.dest_folder  # shutil.make_archive appends ".zip" itself
    zip_path = shutil.make_archive(zip_base, "zip", root_dir=result.dest_folder)

    shutil.rmtree(result.dest_folder, ignore_errors=True)

    job.zip_path = zip_path
    job.succeeded_tracks = result.succeeded_tracks
    job.failed_tracks = result.failed_tracks
    job.total_tracks = len(result.succeeded_tracks) + len(result.failed_tracks)
    job.progress = (
        "Done."
        if not result.failed_tracks
        else f"Done, but {len(result.failed_tracks)} of {job.total_tracks} track(s) could not be downloaded."
    )


def remove_job_files(job: Job) -> None:
    """Delete everything on disk for an expired job (called by jobs.cleanup_loop)."""
    shutil.rmtree(_job_dir(job), ignore_errors=True)
