"""
Authentication helpers for SearchX.

Tokens: itsdangerous URLSafeTimedSerializer → signed, tamper-proof, expiry-aware.
Stored:  HttpOnly; SameSite=Lax cookie named 'sx_tok'.
Passwords: werkzeug.security pbkdf2-sha256 (already a Flask dependency).
"""

import functools
import os

from flask import g, request, redirect, url_for, abort
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.security import generate_password_hash, check_password_hash

_SECRET      = os.environ.get("SEARCHX_SECRET", "searchx-local-secret-change-me")
_SALT        = "sx-auth"
_TTL_SECONDS = 30 * 24 * 3600   # 30-day tokens
_COOKIE      = "sx_tok"

_signer = URLSafeTimedSerializer(_SECRET)


# ── Password helpers ────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, hashed: str) -> bool:
    return check_password_hash(hashed, password)


# ── Token helpers ───────────────────────────────────────────────────────────

def create_token(user_id: int, role: str) -> str:
    return _signer.dumps({"id": user_id, "role": role}, salt=_SALT)


def decode_token(token: str) -> dict | None:
    try:
        return _signer.loads(token, salt=_SALT, max_age=_TTL_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def set_auth_cookie(response, user_id: int, role: str):
    token = create_token(user_id, role)
    response.set_cookie(
        _COOKIE, token,
        max_age=_TTL_SECONDS,
        httponly=True,
        samesite="Lax",
        secure=False,   # set True if you add HTTPS
    )
    return response


def clear_auth_cookie(response):
    response.delete_cookie(_COOKIE)
    return response


# ── Current user ─────────────────────────────────────────────────────────────

def load_user() -> dict | None:
    """Decode the cookie and return the user dict, or None."""
    token = request.cookies.get(_COOKIE)
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return payload   # {"id": int, "role": str}


def current_user() -> dict | None:
    """Return the current user from Flask g (populated by @login_required)."""
    return getattr(g, "_sx_user", None)


# ── Decorators ────────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = load_user()
        if not user:
            return redirect(url_for("login_page", next=request.path))
        g._sx_user = user
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        user = load_user()
        if not user:
            return redirect(url_for("login_page", next=request.path))
        if user.get("role") != "admin":
            abort(403)
        g._sx_user = user
        return f(*args, **kwargs)
    return wrapper


def optional_user(f):
    """Attach user to g if logged in, but don't redirect if not."""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        g._sx_user = load_user()
        return f(*args, **kwargs)
    return wrapper
