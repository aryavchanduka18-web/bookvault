"""JWT authentication and role-based access control (Module 4, session 28).

Two layers of access control exist in this project and they are different
things:

  APPLICATION roles (here)   student / librarian / admin, stored on the user
                             document and carried in a JWT. Decides which
                             HTTP endpoints a logged-in person may call.

  DATABASE roles (Atlas)     readWrite / read, configured in Atlas under
                             Database Access. Decides what the connection
                             itself may do, regardless of this code.

The report should mention both: even if someone bypassed Flask entirely,
the Atlas user still cannot do more than its database role allows.
"""

from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import g, request

from app.config import get_settings
from app.core.errors import ApiError
from app.core.mongoshell import call, record
from app.db import USERS, collection

ROLE_HIERARCHY = ["student", "librarian", "admin"]


def create_token(user: dict) -> dict:
    """Sign a JWT for a user document."""
    settings = get_settings()
    issued = datetime.now(timezone.utc)
    expires = issued + timedelta(minutes=settings.jwt_expire_minutes)

    payload = {
        "sub": str(user["_id"]),
        "email": user["email"],
        "name": user["name"],
        "role": user["role"],
        "iat": issued,
        "exp": expires,
    }

    token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)

    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": settings.jwt_expire_minutes * 60,
        "expires_at": expires.isoformat(),
    }


def decode_token(token: str) -> dict:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "token expired", "log in again")
    except jwt.InvalidTokenError as exc:
        raise ApiError(401, "invalid token", str(exc))


def _token_from_request() -> str:
    header = request.headers.get("Authorization", "")
    if not header:
        raise ApiError(401, "authentication required", "send: Authorization: Bearer <token>")

    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise ApiError(401, "malformed Authorization header", "expected: Bearer <token>")

    return token.strip()


def require_auth(view):
    """Any logged-in user may proceed. Puts the claims on flask.g."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        g.user = decode_token(_token_from_request())
        return view(*args, **kwargs)

    return wrapper


def require_role(*allowed: str):
    """Only these roles may proceed. Implies require_auth."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            claims = decode_token(_token_from_request())
            g.user = claims

            if claims.get("role") not in allowed:
                raise ApiError(
                    403,
                    "insufficient role",
                    f"this endpoint needs one of {list(allowed)}; "
                    f"you are '{claims.get('role')}'",
                )
            return view(*args, **kwargs)

        return wrapper

    return decorator


def authenticate(email: str, password: str) -> dict:
    """Look up a user and check the password. Raises 401 on any failure."""
    from app.core.security import verify_password

    lookup = {"email": email.strip().lower()}
    record(
        "Look up the account by email (served by the email_unique index). "
        "MongoDB only finds the account; the password is checked afterwards in Python with bcrypt",
        call(USERS, "findOne", lookup),
    )
    user = collection(USERS).find_one(lookup)

    # The same message whether the email is unknown or the password is
    # wrong, so this endpoint cannot be used to discover which addresses
    # are registered.
    if user is None or not verify_password(password, user.get("password_hash", "")):
        raise ApiError(401, "invalid email or password")

    return user
