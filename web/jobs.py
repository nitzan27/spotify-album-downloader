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

# A rate_limited scan auto-requeues itself (same job id, so the frontend's
# poll of /status/{id} just sees it go rate_limited -> queued -> running
# again) up to this many times before requiring an explicit "Resume scan"
# click. Delay honors Spotify's Retry-After when given, capped so one big
# Retry-After can't stall the single scan worker for an unreasonable time.
MAX_SCAN_AUTO_RETRIES = 3
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

    # scan-only
    session_id: Optional[str] = None
    market: Optional[str] = None
    results: Optional[list[dict]] = None  # final sync-queue-shaped output when done
    retry_count: int = 0  # automatic rate_limited requeues so far, capped at MAX_SCAN_AUTO_RETRIES
    retry_after_seconds: Optional[float] = None  # set by process_scan_job when rate_limited


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


def create_scan_job(session_id: str, market: str) -> Job:
    job = Job(id=str(uuid.uuid4()), job_type="scan", session_id=session_id, market=market)
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
    """Requeue a rate_limited scan job (same id) after a backoff, up to MAX_SCAN_AUTO_RETRIES.

    Runs as a background asyncio task (not a blocking sleep in the worker
    loop) so one job's backoff can't stall every other friend's scan behind
    it in the single-worker scan queue.
    """
    if job.job_type != "scan" or job.retry_count >= MAX_SCAN_AUTO_RETRIES:
        return
    delay = min(job.retry_after_seconds or DEFAULT_AUTO_RETRY_SECONDS, MAX_AUTO_RETRY_DELAY_SECONDS)
    job.retry_count += 1
    asyncio.create_task(_requeue_after(job, queue, delay))


async def _requeue_after(job: Job, queue: "asyncio.Queue[str]", delay: float) -> None:
    await asyncio.sleep(delay)
    if _is_expired(job) or job.id not in JOBS:
        return
    job.status = "queued"
    job.progress = f"{job.progress} Retrying automatically (attempt {job.retry_count}/{MAX_SCAN_AUTO_RETRIES})..."
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
