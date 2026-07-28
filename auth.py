"""HTTP Basic Auth for the admin UI and its REST API.

Protects ``/admin`` and ``/api`` only. Twilio webhooks, the browser test
client (``/``, ``/offer``), and static assets stay public.

Credentials come from env:

  ADMIN_USERNAME   (default: ``admin``)
  ADMIN_PASSWORD   (required to enable auth)

If ``ADMIN_PASSWORD`` is empty/unset, auth is **disabled** and a warning
is logged at import time. That keeps local `python server.py` frictionless
while forcing production to set a Fly secret.
"""

from __future__ import annotations

import os
import secrets
from base64 import b64decode

from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

_PROTECTED_PREFIXES = ("/admin", "/api")

# Realm shown in the browser's native Basic Auth dialog.
_REALM = "Funkle Admin"


def _expected_credentials() -> tuple[str, str] | None:
    """Return (username, password) when auth is enabled, else None."""
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not password:
        return None
    username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    return username, password


def auth_enabled() -> bool:
    return _expected_credentials() is not None


def _unauthorized() -> Response:
    return Response(
        content="Authentication required.",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{_REALM}"'},
        media_type="text/plain",
    )


def _path_is_protected(path: str) -> bool:
    # Exact /admin or /api, or anything under them (/admin/..., /api/...).
    for prefix in _PROTECTED_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _parse_basic(header: str | None) -> tuple[str, str] | None:
    """Decode ``Authorization: Basic …`` into (username, password), or None."""
    if not header:
        return None
    scheme, _, param = header.partition(" ")
    if scheme.lower() != "basic" or not param:
        return None
    try:
        decoded = b64decode(param.strip()).decode("utf-8")
    except Exception:
        return None
    # Username may not contain ':', password may — split once.
    if ":" not in decoded:
        return None
    user, _, password = decoded.partition(":")
    return user, password


class AdminBasicAuthMiddleware(BaseHTTPMiddleware):
    """Reject unauthenticated requests to ``/admin`` and ``/api``.

    When ``ADMIN_PASSWORD`` is unset the middleware is a no-op so local
    development keeps working without secrets.
    """

    async def dispatch(self, request: Request, call_next):
        expected = _expected_credentials()
        if expected is None or not _path_is_protected(request.url.path):
            return await call_next(request)

        # CORS preflight never carries Authorization — let it through so
        # the CORS middleware can answer; real requests still need creds.
        if request.method == "OPTIONS":
            return await call_next(request)

        provided = _parse_basic(request.headers.get("authorization"))
        if provided is None:
            return _unauthorized()

        exp_user, exp_pass = expected
        got_user, got_pass = provided
        user_ok = secrets.compare_digest(got_user, exp_user)
        pass_ok = secrets.compare_digest(got_pass, exp_pass)
        if not (user_ok and pass_ok):
            return _unauthorized()

        return await call_next(request)


def log_auth_status() -> None:
    """Call once at startup so ops can see whether the gate is armed."""
    if auth_enabled():
        user = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
        logger.info(f"Admin Basic Auth ENABLED (user={user!r}) for /admin and /api")
    else:
        logger.warning(
            "Admin Basic Auth DISABLED — ADMIN_PASSWORD is not set. "
            "/admin and /api are publicly reachable. "
            "Set ADMIN_PASSWORD (and optionally ADMIN_USERNAME) before production use."
        )
