"""Per-user Spotify login (Authorization Code flow) for the web app.

Distinct from album_downloader.py's Client Credentials flow (app-only, no
/me/* access) and from sync_missing.py's CLI-only get_user_spotify_client()
(local file cache, interactive open-browser flow, sys.exit on missing creds -
none of that fits a server handling requests from many different friends).

CSRF note: spotipy's parse_response_code()/get_access_token() never validate
the OAuth `state` param themselves - that check only lives inside spotipy's
own interactive/local-server helpers, which this module doesn't use. Without
doing it ourselves, an attacker could get a victim to bind their session to
the attacker's Spotify account (login CSRF). start_login()/finish_login()
below handle this explicitly.
"""

import os
import secrets

import spotipy
from spotipy.cache_handler import CacheHandler
from spotipy.oauth2 import SpotifyOAuth

from album_downloader import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from sync_missing import USER_SCOPE
from web.sessions import Session

# Deliberately a separate env var from sync_missing.py's SPOTIFY_REDIRECT_URI
# (that one is the CLI's own loopback address). This one must point at
# wherever the web app is actually reachable (e.g. the deployed Render URL),
# registered as an additional Redirect URI on the same Spotify app.
SPOTIFY_WEB_REDIRECT_URI = os.environ.get("SPOTIFY_WEB_REDIRECT_URI", "http://127.0.0.1:8000/callback")


class SessionCacheHandler(CacheHandler):
    """Reads/writes a token_info dict on a Session object instead of a file or
    in-memory instance attribute, so a fresh SpotifyOAuth built per-request
    still sees (and persists refreshes to) the same session's token."""

    def __init__(self, session: Session):
        self._session = session

    def get_cached_token(self):
        return self._session.token_info

    def save_token_to_cache(self, token_info):
        self._session.token_info = token_info


def build_oauth(session: Session) -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_WEB_REDIRECT_URI,
        scope=USER_SCOPE,
        cache_handler=SessionCacheHandler(session),
        open_browser=False,
    )


def start_login(session: Session) -> str:
    """Generate+store a CSRF nonce and return the Spotify authorize URL to redirect to."""
    nonce = secrets.token_urlsafe(24)
    session.oauth_state = nonce
    return build_oauth(session).get_authorize_url(state=nonce)


def finish_login(session: Session, code: str, state: str) -> None:
    """Validate the callback's `state` against what start_login() stored, then
    exchange `code` for a token (persisted onto `session` via SessionCacheHandler).

    Raises ValueError on a missing/mismatched state - callers should treat
    that as a 400, not proceed to exchange the code.
    """
    if not session.oauth_state or not secrets.compare_digest(state or "", session.oauth_state):
        raise ValueError("OAuth state mismatch - possible CSRF, login aborted.")
    session.oauth_state = None
    build_oauth(session).get_access_token(code, check_cache=False)


def get_authenticated_client(session: Session) -> spotipy.Spotify | None:
    """Return a Spotify client for this session if it has a logged-in token, else None."""
    if not session.token_info:
        return None
    # retries=0: same reasoning as album_downloader.get_spotify_client() and
    # sync_missing.get_user_spotify_client() - fail fast on a 429 instead of
    # spotipy silently sleeping for Retry-After (which can be hours).
    # status_retries=0 too: spotipy's retries=0 only zeroes urllib3's overall
    # `total` retry budget - status-code retries (429/500/502/503/504) are a
    # SEPARATE counter that defaults to 3 regardless of `retries`. Left at
    # its default, a run of 502s still gets silently retried a few times
    # with backoff before finally raising - confirmed live during a large
    # library scan (95 playlists), which cost real time before failing.
    return spotipy.Spotify(auth_manager=build_oauth(session), retries=0, status_retries=0)
