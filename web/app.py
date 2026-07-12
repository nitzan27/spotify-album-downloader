"""FastAPI web frontend for album_downloader.download_album().

Run locally with: uvicorn web.app:app --reload --port 8000
Run in production with a single worker (job state is in-process memory):
    uvicorn web.app:app --host 0.0.0.0 --port $PORT --workers 1

Every route is gated by HTTP Basic Auth (see web/auth.py) except /healthz,
via the router-level dependency below - a new route added later is gated
by construction instead of relying on remembering to add auth per-route.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from web import jobs
from web.auth import require_auth
from web.downloader_adapter import process_job, remove_job_files

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.set_worker(process_job)
    for _ in range(jobs.WORKER_COUNT):
        _background_tasks.append(asyncio.create_task(jobs.worker_loop()))
    _background_tasks.append(asyncio.create_task(jobs.cleanup_loop(remove_job_files)))

    yield

    for task in _background_tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


router = APIRouter(dependencies=[Depends(require_auth)])


class NewJobRequest(BaseModel):
    artist: str
    album: str


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html")


@router.post("/jobs")
def submit_job(payload: NewJobRequest):
    artist = payload.artist.strip()
    album = payload.album.strip()
    if not artist or not album:
        raise HTTPException(status_code=400, detail="Artist and album are required.")
    job = jobs.create_job(artist, album)
    return {"job_id": job.id}


@router.get("/status/{job_id}")
def job_status(job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return {"status": job.status, "progress": job.progress, "error": job.error}


@router.get("/download/{job_id}")
def download(job_id: str):
    job = jobs.get_job(job_id)
    if job is None or job.status != "done" or not job.zip_path:
        raise HTTPException(status_code=404, detail="No completed download for this job.")
    return FileResponse(
        job.zip_path, filename=os.path.basename(job.zip_path), media_type="application/zip"
    )


app.include_router(router)
