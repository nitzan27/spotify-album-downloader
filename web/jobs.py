"""In-memory job tracking and a small bounded worker pool for album downloads.

Kept intentionally simple (no Celery/Redis) since this serves a small
invite-only friend group, not public traffic - see CLAUDE.md for the
reasoning. Job state lives in a single process's memory, which is why the
app must run with a single uvicorn worker (`--workers 1`): a status poll
landing on a different worker process would never find the job.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from starlette.concurrency import run_in_threadpool

JOB_TTL_SECONDS = 60 * 60  # expire jobs (and their files) after 1 hour
WORKER_COUNT = 2  # concurrency cap - each job spawns its own yt-dlp/ffmpeg subprocesses


@dataclass
class Job:
    id: str
    artist: str
    album: str
    status: str = "queued"  # queued -> running -> done | error
    progress: str = ""
    error: Optional[str] = None
    zip_path: Optional[str] = None
    created_at: float = field(default_factory=time.time)


JOBS: dict[str, Job] = {}
_queue: "asyncio.Queue[str]" = asyncio.Queue()
_worker_fn: Optional[Callable[[Job], None]] = None


def set_worker(fn: Callable[[Job], None]) -> None:
    """Register the (sync) function each worker task calls to actually process a job."""
    global _worker_fn
    _worker_fn = fn


def create_job(artist: str, album: str) -> Job:
    job = Job(id=str(uuid.uuid4()), artist=artist, album=album)
    JOBS[job.id] = job
    _queue.put_nowait(job.id)
    return job


def get_job(job_id: str) -> Optional[Job]:
    job = JOBS.get(job_id)
    if job is None or _is_expired(job):
        return None
    return job


def _is_expired(job: Job) -> bool:
    return time.time() - job.created_at > JOB_TTL_SECONDS


async def worker_loop() -> None:
    """Long-lived consumer: pulls job ids off the queue and runs them one at a time."""
    while True:
        job_id = await _queue.get()
        job = JOBS.get(job_id)
        if job is None:
            _queue.task_done()
            continue

        job.status = "running"
        try:
            if _worker_fn is None:
                raise RuntimeError("No worker function registered")
            await run_in_threadpool(_worker_fn, job)
            job.status = "done"
        except Exception as exc:
            job.status = "error"
            job.error = str(exc)
        finally:
            _queue.task_done()


async def cleanup_loop(remove_files: Callable[[Job], None], interval_seconds: int = 300) -> None:
    """Periodically evict expired jobs from the registry, letting the caller delete their files."""
    while True:
        await asyncio.sleep(interval_seconds)
        expired_ids = [job_id for job_id, job in list(JOBS.items()) if _is_expired(job)]
        for job_id in expired_ids:
            job = JOBS.pop(job_id, None)
            if job is not None:
                remove_files(job)
