"""FastAPI web frontend for album_downloader.download_album().

Run locally with: uvicorn web.app:app --reload --port 8000
Run in production with a single worker (job state is in-process memory):
    uvicorn web.app:app --host 0.0.0.0 --port $PORT --workers 1

No login gate - access control is just not sharing the URL with anyone
but the intended friends. See web/CLAUDE.md for the trade-off this implies.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from spotipy.oauth2 import SpotifyOauthError

from web import jobs, sessions
from web.downloader_adapter import process_job, remove_job_files
from web.scan_adapter import process_scan_job, resume_scan_job
from web.spotify_auth import finish_login, get_authenticated_client, start_login

# The React/TS SPA build (web/frontend/, built via `npm run build`) - built
# by hand for local dev, built inside the Dockerfile's Node stage in
# production (the final image itself stays Node-free at runtime).
FRONTEND_DIST = os.path.join(os.path.dirname(__file__), "frontend", "dist")

SESSION_COOKIE_NAME = "sid"

_background_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    jobs.set_download_worker(process_job)
    for _ in range(jobs.DOWNLOAD_WORKER_COUNT):
        _background_tasks.append(asyncio.create_task(jobs.download_worker_loop()))
    jobs.set_scan_worker(process_scan_job)
    for _ in range(jobs.SCAN_WORKER_COUNT):
        _background_tasks.append(asyncio.create_task(jobs.scan_worker_loop()))
    _background_tasks.append(asyncio.create_task(jobs.cleanup_loop(remove_job_files)))
    _background_tasks.append(asyncio.create_task(sessions.cleanup_loop()))

    yield

    for task in _background_tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


router = APIRouter()


def _get_session(request: Request) -> Optional[sessions.Session]:
    return sessions.get_session(request.cookies.get(SESSION_COOKIE_NAME))


def _set_session_cookie(response: Response, request: Request, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        httponly=True,
        samesite="lax",
        # Only assert `secure` when actually served over HTTPS (Render's
        # deployed URL) - otherwise local http://127.0.0.1 dev breaks, since
        # browsers won't send a `secure` cookie back over plain HTTP.
        secure=request.url.scheme == "https",
        max_age=sessions.SESSION_TTL_SECONDS,
    )


class NewJobRequest(BaseModel):
    artist: str
    album: str


@router.get("/")
def index():
    # No caching for the SPA shell itself - every `npm run build` replaces
    # the hashed JS/CSS filenames it references, so a browser-cached copy of
    # an older index.html can point at asset files that no longer exist on
    # disk (a silent 404 on a `type="module"` script = a blank page, since
    # the browser refuses to execute it and nothing else runs). The hashed
    # asset files themselves are safe to cache aggressively - only this
    # shell document needs to always be fetched fresh.
    return FileResponse(os.path.join(FRONTEND_DIST, "index.html"), headers={"Cache-Control": "no-store"})


@router.get("/favicon.png")
def favicon():
    return FileResponse(os.path.join(FRONTEND_DIST, "favicon.png"))


@router.get("/privacy")
def privacy():
    # A static page, not part of the React build - exists mainly to have a
    # URL ready for Spotify's Extension Request form (see web/CLAUDE.md).
    return FileResponse(os.path.join(os.path.dirname(__file__), "privacy.html"))


@router.get("/login")
def login(request: Request):
    session = _get_session(request) or sessions.create_session()
    authorize_url = start_login(session)
    response = RedirectResponse(authorize_url)
    _set_session_cookie(response, request, session.id)
    return response


@router.get("/callback")
def callback(request: Request, code: str = "", state: str = "", error: str = ""):
    session = _get_session(request)
    if session is None:
        raise HTTPException(status_code=400, detail="No active login session; start again at /login.")
    if error:
        raise HTTPException(status_code=400, detail=f"Spotify login was not completed: {error}")
    try:
        finish_login(session, code, state)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SpotifyOauthError as exc:
        # The code<->token exchange itself failed (expired/reused code, a
        # redirect_uri mismatch between /login and this request, bad
        # client secret, etc.) - surface Spotify's actual reason instead of
        # a bare 500, since that's almost always what's actionable here.
        raise HTTPException(status_code=400, detail=f"Spotify token exchange failed: {exc}") from exc
    return RedirectResponse("/")


@router.post("/logout")
def logout(request: Request):
    session = _get_session(request)
    if session is not None:
        session.token_info = None
        session.oauth_state = None
        session.display_name = None
        session.spotify_user_id = None
    response = Response(status_code=204)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@router.get("/me")
def me(request: Request):
    session = _get_session(request)
    if session is None or not session.token_info:
        return {"logged_in": False, "display_name": None, "avatar_url": None}

    if session.display_name is None:
        sp = get_authenticated_client(session)
        try:
            profile = sp.current_user()
            session.display_name = profile.get("display_name") or profile.get("id")
            session.spotify_user_id = profile.get("id")
            images = profile.get("images") or []
            session.avatar_url = images[0]["url"] if images else None
        except Exception:
            pass  # token may be invalid/revoked; still report logged_in based on stored token_info

    return {"logged_in": True, "display_name": session.display_name, "avatar_url": session.avatar_url}


class ScanRequest(BaseModel):
    market: str


@router.post("/scan")
def start_scan(request: Request, payload: ScanRequest):
    session = _get_session(request)
    if session is None or not session.token_info:
        raise HTTPException(status_code=401, detail="Log in with Spotify first.")
    market = payload.market.strip().upper()
    if len(market) != 2 or not market.isalpha():
        raise HTTPException(status_code=400, detail="Market must be a 2-letter country code (e.g. US, IL).")
    job = jobs.create_scan_job(session.id, market)
    return {"job_id": job.id}


@router.post("/scan/{job_id}/resume")
def resume_scan(request: Request, job_id: str):
    session = _get_session(request)
    old_job = jobs.get_job(job_id)
    if old_job is None or old_job.job_type != "scan" or session is None or old_job.session_id != session.id:
        raise HTTPException(status_code=404, detail="Scan job not found.")
    if old_job.status != "rate_limited":
        raise HTTPException(status_code=400, detail="Only a rate-limited scan can be resumed.")
    new_job = resume_scan_job(old_job)
    return {"job_id": new_job.id}


@router.post("/jobs")
def submit_job(payload: NewJobRequest):
    artist = payload.artist.strip()
    album = payload.album.strip()
    if not artist or not album:
        raise HTTPException(status_code=400, detail="Artist and album are required.")
    job = jobs.create_download_job(artist, album)
    return {"job_id": job.id}


class BatchJobRequest(BaseModel):
    albums: list[NewJobRequest]


@router.post("/jobs/batch")
def submit_jobs_batch(payload: BatchJobRequest):
    if not payload.albums:
        raise HTTPException(status_code=400, detail="No albums provided.")
    # No cap on batch size: jobs.DOWNLOAD_WORKER_COUNT already bounds how many
    # of these actually download concurrently - queuing 300 jobs here just
    # means 298 of them sit in the queue behind the first 2, not that 300
    # downloads happen at once. A request-size cap here would only reject a
    # big "download selected" from a scan for no real protective reason.
    job_ids = []
    for entry in payload.albums:
        artist = entry.artist.strip()
        album = entry.album.strip()
        if not artist or not album:
            raise HTTPException(status_code=400, detail="Artist and album are required for every entry.")
        job_ids.append(jobs.create_download_job(artist, album).id)
    return {"job_ids": job_ids}


@router.get("/status/{job_id}")
def job_status(request: Request, job_id: str):
    job = jobs.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    # Lets the frontend know whether a rate_limited job may still auto-requeue
    # itself (keep polling) - see jobs.MAX_AUTO_RETRIES. For a scan, "false"
    # additionally means a manual "Resume scan" click is needed; a download
    # has no such button - once exhausted it self-resolves to "error" instead
    # (see jobs._schedule_auto_retry), so this is informational only there.
    will_auto_retry = job.status == "rate_limited" and job.retry_count < jobs.MAX_AUTO_RETRIES
    if job.job_type == "scan":
        # Scan results are someone's private library contents - unlike anonymous
        # download jobs, only the session that started this scan may read it.
        session = _get_session(request)
        if session is None or job.session_id != session.id:
            raise HTTPException(status_code=404, detail="Job not found.")
        return {
            "status": job.status,
            "progress": job.progress,
            "error": job.error,
            "results": job.results,
            "will_auto_retry": will_auto_retry,
            "succeeded_tracks": job.succeeded_tracks,
            "failed_tracks": job.failed_tracks,
            "total_tracks": job.total_tracks,
        }
    return {
        "status": job.status,
        "progress": job.progress,
        "error": job.error,
        "will_auto_retry": will_auto_retry,
        "succeeded_tracks": job.succeeded_tracks,
        "failed_tracks": job.failed_tracks,
        "total_tracks": job.total_tracks,
    }


@router.get("/download/{job_id}")
def download(job_id: str):
    job = jobs.get_job(job_id)
    if job is None or job.status != "done" or not job.zip_path:
        raise HTTPException(status_code=404, detail="No completed download for this job.")
    return FileResponse(
        job.zip_path, filename=os.path.basename(job.zip_path), media_type="application/zip"
    )


app.include_router(router)
