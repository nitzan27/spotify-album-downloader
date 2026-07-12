"""Shared-secret gate for the friends-only site (HTTP Basic Auth).

Only meaningful over HTTPS (Render's free *.onrender.com domains provide
this automatically) - Basic Auth credentials are base64, not encrypted.
Applied at the router/app level (see web/app.py) so every route is gated
by construction instead of relying on remembering to add it per-route.
"""

import os
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_security = HTTPBasic()

SITE_USERNAME = os.environ.get("SITE_USERNAME", "")
SITE_PASSWORD = os.environ.get("SITE_PASSWORD", "")


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    if not SITE_USERNAME or not SITE_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server is missing SITE_USERNAME/SITE_PASSWORD configuration.",
        )

    valid_username = secrets.compare_digest(credentials.username, SITE_USERNAME)
    valid_password = secrets.compare_digest(credentials.password, SITE_PASSWORD)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )
