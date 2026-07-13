"""In-memory per-user session registry (Spotify login state).

Mirrors web/jobs.py's registry pattern: a plain dict keyed by an opaque id,
evicted on a TTL by a periodic cleanup task. Session state (Spotify token,
OAuth CSRF nonce) lives here, server-side - the client only ever holds the
opaque `sid` cookie value used as the dict key, never the token itself.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

# Deliberately longer than jobs.JOB_TTL_SECONDS (1hr): a login must survive a
# possibly-long rate-limited scan, reviewing results, and watching several
# downloads finish - not just "one zip sitting around waiting to be fetched."
SESSION_TTL_SECONDS = 6 * 60 * 60


@dataclass
class Session:
    id: str
    token_info: Optional[dict] = None
    oauth_state: Optional[str] = None
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    spotify_user_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)


SESSIONS: dict[str, Session] = {}


def create_session() -> Session:
    session = Session(id=str(uuid.uuid4()))
    SESSIONS[session.id] = session
    return session


def get_session(session_id: Optional[str]) -> Optional[Session]:
    if not session_id:
        return None
    session = SESSIONS.get(session_id)
    if session is None or _is_expired(session):
        return None
    return session


def delete_session(session_id: Optional[str]) -> None:
    if session_id:
        SESSIONS.pop(session_id, None)


def _is_expired(session: Session) -> bool:
    return time.time() - session.created_at > SESSION_TTL_SECONDS


async def cleanup_loop(interval_seconds: int = 300) -> None:
    """Periodically evict expired sessions, mirroring jobs.cleanup_loop."""
    while True:
        await asyncio.sleep(interval_seconds)
        expired_ids = [sid for sid, session in list(SESSIONS.items()) if _is_expired(session)]
        for sid in expired_ids:
            SESSIONS.pop(sid, None)
