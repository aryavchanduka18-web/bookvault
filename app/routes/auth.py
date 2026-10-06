"""Login and the permission matrix (Module 4, session 28)."""

from flask import Blueprint, g, jsonify, request

from app.core.auth import authenticate, create_token, require_auth
from app.core.validation import field_str, reject_unknown, require_body

bp = Blueprint("auth", __name__, url_prefix="/auth")


# Written out so a demo can show the whole access-control design on one
# screen, and so the report has something concrete to reproduce.
PERMISSIONS = {
    "public (no token)": [
        "GET /books, /books/<id>, /books/explain",
        "GET /analytics/*",
        "GET /stream/events, /stream/demo",
    ],
    "student": [
        "everything public",
        "POST /borrow, /borrow/return, /borrow/demo-failure",
        "GET /borrow, GET /users/<id>/history",
    ],
    "librarian": [
        "everything a student can do",
        "POST /books, PATCH /books/<id>, DELETE /books/<id>",
        "GET /users, GET /users/<id>, POST /users",
    ],
    "admin": [
        "everything a librarian can do",
        "PATCH /users/<id>, DELETE /users/<id>",
    ],
}


@bp.post("/login")
def login():
    """Exchange email + password for a JWT."""
    data = require_body(request.get_json(silent=True))
    reject_unknown(data, {"email", "password"})

    email = field_str(data, "email", max_len=200)
    password = field_str(data, "password", min_len=1, max_len=200)

    user = authenticate(email, password)
    token = create_token(user)

    return jsonify({
        **token,
        "user": {
            "id": str(user["_id"]),
            "name": user["name"],
            "email": user["email"],
            "role": user["role"],
            "department": user.get("department"),
        },
    })


@bp.get("/me")
@require_auth
def me():
    """Whoever the current token belongs to - proves the JWT decoded."""
    return jsonify({"claims": g.user})


@bp.get("/permissions")
def permissions():
    """The role matrix. No token needed - it is documentation."""
    return jsonify({
        "application_roles": PERMISSIONS,
        "note": (
            "These are APPLICATION roles carried in the JWT. They are separate "
            "from the DATABASE roles configured in Atlas (readWrite / read), "
            "which constrain the connection itself no matter what this code does."
        ),
        "login": 'POST /auth/login with {"email": "...", "password": "..."}',
        "then": "send Authorization: Bearer <access_token> on protected routes",
    })
