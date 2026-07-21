"""In-memory job tracking and bounded worker pools for downloads and scans.

Kept intentionally simple (no Celery/Redis) since this serves a small
invite-only friend group, not public traffic - see CLAUDE.md for the
reasoning. Job state lives in a single process's memory, which is why the
app must run with a single uvicorn worker (`--workers 1`): a status poll
landing on a different worker process would never find the job.

One Job dataclass covers both "download" and "scan" jobs (the registry/TTL
plumbing is identical either way), but downloads and scans run through two
SEPARATE queues/worker pools: downloads are ffmpeg/CPU-bound, scans are
long-running and rate-limit-bound, and funneling both through one pool would
let a friend's big library scan head-of-line-block someone else's simple
album download.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from starlette.concurrency import run_in_threadpool

JOB_TTL_SECONDS = 60 * 60  # expire jobs (and their files) after 1 hour
DOWNLOAD_WORKER_COUNT = 2  # concurrency cap - each download job spawns yt-dlp/ffmpeg subprocesses
SCAN_WORKER_COUNT = 1  # scans are rare/one-shot per user; keep concurrent Spotify calls low

# A rate_limited job (scan or download) auto-requeues itself (same job id, so
# the frontend's poll of /status/{id} just sees it go rate_limited -> queued
# -> running again) up to this many times. Delay honors Spotify's
# Retry-After when given, capped so one big Retry-After can't stall a worker
# for an unreasonable time. Once exhausted: a scan sits waiting for an
# explicit "Resume scan" click (it has a durable cache, so nothing is lost by
# waiting); a download has no such resume endpoint, so it fails terminally
# instead - see _schedule_auto_retry.
MAX_AUTO_RETRIES = 3
DEFAULT_AUTO_RETRY_SECONDS = 30  # used when Retry-After wasn't parseable (a 5xx, not a 429)
MAX_AUTO_RETRY_DELAY_SECONDS = 300


@dataclass
class Job:
    id: str
    job_type: str  # "download" | "scan"
    status: str = "queued"  # queued -> running -> done | error | rate_limited (scan only)
    progress: str = ""
    error: Optional[str] = None
    created_at: float = field(default_factory=time.time)

    # download-only
    artist: Optional[str] = None
    album: Optional[str] = None
    zip_path: Optional[str] = None
    # Populated only once status == "done"; empty (not None) means every
    # track succeeded, so the frontend can check `.failed_tracks.length` with
    # no null-check. Deliberately doesn't get its own "partial" status - a
    # download with some failed tracks is still a completed, deliverable job.
    failed_tracks: list[dict] = field(default_factory=list)  # [{"title": str, "reason": str}]
    total_tracks: Optional[int] = None

    # scan-only
    session_id: Optional[str] = None
    market: Optional[str] = None
    results: Optional[list[dict]] = None  # final sync-queue-shaped output when done
    # Cumulative Spotify API calls across every attempt for this logical scan
    # (each auto-retry re-runs process_scan_job under the same job.id, so this
    # lives on the Job - a fresh local counter per attempt can't add up to a
    # true end-to-end total on its own). Carried forward into a new Job by
    # resume_scan_job() when a friend clicks "Resume scan" manually, so the
    # total still covers the whole scan across that too.
    api_call_count: int = 0

    # shared by both job types: automatic rate_limited requeues so far
    # (capped at MAX_AUTO_RETRIES) and the delay to honor before the next one
    retry_count: int = 0
    retry_after_seconds: Optional[float] = None


JOBS: dict[str, Job] = {}
_download_queue: "asyncio.Queue[str]" = asyncio.Queue()
_scan_queue: "asyncio.Queue[str]" = asyncio.Queue()
_download_worker_fn: Optional[Callable[[Job], None]] = None
_scan_worker_fn: Optional[Callable[[Job], None]] = None


def set_download_worker(fn: Callable[[Job], None]) -> None:
    global _download_worker_fn
    _download_worker_fn = fn


def set_scan_worker(fn: Callable[[Job], None]) -> None:
    global _scan_worker_fn
    _scan_worker_fn = fn


def create_download_job(artist: str, album: str) -> Job:
    job = Job(id=str(uuid.uuid4()), job_type="download", artist=artist, album=album)
    JOBS[job.id] = job
    _download_queue.put_nowait(job.id)
    return job


def create_scan_job(session_id: str, market: str, initial_api_call_count: int = 0) -> Job:
    job = Job(
        id=str(uuid.uuid4()),
        job_type="scan",
        session_id=session_id,
        market=market,
        api_call_count=initial_api_call_count,
    )
    JOBS[job.id] = job
    _scan_queue.put_nowait(job.id)
    return job


def get_job(job_id: str) -> Optional[Job]:
    job = JOBS.get(job_id)
    if job is None or _is_expired(job):
        return None
    return job


def _is_expired(job: Job) -> bool:
    return time.time() - job.created_at > JOB_TTL_SECONDS


async def _run_worker_loop(queue: "asyncio.Queue[str]", get_worker_fn: Callable[[], Optional[Callable[[Job], None]]]) -> None:
    """Long-lived consumer: pulls job ids off `queue` and runs them one at a time."""
    while True:
        job_id = await queue.get()
        job = JOBS.get(job_id)
        if job is None:
            queue.task_done()
            continue

        job.status = "running"
        try:
            worker_fn = get_worker_fn()
            if worker_fn is None:
                raise RuntimeError("No worker function registered")
            await run_in_threadpool(worker_fn, job)
            if job.status == "running":  # workers may set "rate_limited" themselves
                job.status = "done"
            elif job.status == "rate_limited":
                _schedule_auto_retry(job, queue)
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
        finally:
            queue.task_done()


def _schedule_auto_retry(job: Job, queue: "asyncio.Queue[str]") -> None:
    """Requeue a rate_limited job (same id) after a backoff, up to MAX_AUTO_RETRIES.

    Runs as a background asyncio task (not a blocking sleep in the worker
    loop) so one job's backoff can't stall every other job behind it in its
    (single- or multi-worker) queue.
    """
    if job.retry_count >= MAX_AUTO_RETRIES:
        if job.job_type == "download":
            # Unlike a scan (which keeps its durable per-user cache and just
            # waits for a manual "Resume scan" click), a download job has no
            # resume endpoint - leaving it stuck at "rate_limited" forever
            # would show nothing useful, so fail it terminally instead.
            job.status = "error"
            job.error = f"Spotify rate limit persisted after {MAX_AUTO_RETRIES} automatic retries - try again in a few minutes."
        return
    delay = min(job.retry_after_seconds or DEFAULT_AUTO_RETRY_SECONDS, MAX_AUTO_RETRY_DELAY_SECONDS)
    job.retry_count += 1
    asyncio.create_task(_requeue_after(job, queue, delay))


async def _requeue_after(job: Job, queue: "asyncio.Queue[str]", delay: float) -> None:
    await asyncio.sleep(delay)
    if _is_expired(job) or job.id not in JOBS:
        return
    job.status = "queued"
    job.progress = f"{job.progress} Retrying automatically (attempt {job.retry_count}/{MAX_AUTO_RETRIES})..."
    queue.put_nowait(job.id)


async def download_worker_loop() -> None:
    await _run_worker_loop(_download_queue, lambda: _download_worker_fn)


async def scan_worker_loop() -> None:
    await _run_worker_loop(_scan_queue, lambda: _scan_worker_fn)


async def cleanup_loop(remove_files: Callable[[Job], None], interval_seconds: int = 300) -> None:
    """Periodically evict expired jobs from the registry, letting the caller delete their files."""
    while True:
        await asyncio.sleep(interval_seconds)
        expired_ids = [job_id for job_id, job in list(JOBS.items()) if _is_expired(job)]
        for job_id in expired_ids:
            job = JOBS.pop(job_id, None)
            if job is not None:
                remove_files(job)
