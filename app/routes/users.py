"""Users CRUD plus borrow history.

GET /users/<id>/history is where the Extended Reference Pattern pays off:
each borrow_record already carries the book title and authors, so a user's
full history renders with ONE query and no $lookup at all.
"""

from datetime import datetime, timezone

from flask import Blueprint, jsonify, request
from pymongo import DESCENDING, ReturnDocument

from app.core.auth import require_auth, require_role
from app.core.errors import ApiError
from app.core.schema import MEMBERSHIPS, ROLES
from app.core.security import hash_password
from app.core.serializers import doc_out, to_object_id
from app.core.validation import (
    MISSING,
    arg_int,
    field_enum,
    field_str,
    reject_unknown,
    require_body,
)
from app.db import BORROW_RECORDS, USERS, collection

bp = Blueprint("users", __name__, url_prefix="/users")

USER_FIELDS = {"name", "email", "department", "membership", "role", "password"}

# password_hash must never leave the server.
PUBLIC = {"password_hash": 0}


def _user_payload(partial: bool) -> dict:
    data = require_body(request.get_json(silent=True))
    reject_unknown(data, USER_FIELDS)

    required = not partial
    out: dict = {}

    def keep(key, value):
        if value is not MISSING:
            out[key] = value

    keep("name", field_str(data, "name", required=required, max_len=120))
    keep("email", field_str(data, "email", required=required, max_len=200))
    keep("department", field_str(data, "department", required=required, max_len=100))
    keep("membership", field_enum(data, "membership", MEMBERSHIPS, required=required))
    keep("role", field_enum(data, "role", ROLES, required=required))

    if "email" in out:
        # Cheap sanity check; the $jsonSchema validator enforces the real
        # pattern inside the database.
        if "@" not in out["email"] or "." not in out["email"].split("@")[-1]:
            raise ApiError(400, "validation failed", "'email' must be a valid email address")
        out["email"] = out["email"].strip().lower()

    password = field_str(data, "password", required=required, min_len=8, max_len=200)
    if password is not MISSING:
        out["password_hash"] = hash_password(password)

    if partial and not out:
        raise ApiError(400, "no fields supplied")

    return out


@bp.get("")
@require_role("librarian", "admin")
def list_users():
    query: dict = {}
    for param in ("department", "membership", "role"):
        value = request.args.get(param)
        if value:
            query[param] = value

    limit = arg_int(request.args, "limit", default=20, minimum=1, maximum=100)
    skip = arg_int(request.args, "skip", default=0, minimum=0)

    cursor = (
        collection(USERS)
        .find(query, PUBLIC)
        .sort([("joined_at", DESCENDING)])
        .skip(skip)
        .limit(limit)
    )

    return jsonify(
        {
            "total": collection(USERS).count_documents(query),
            "skip": skip,
            "limit": limit,
            "items": [doc_out(doc) for doc in cursor],
        }
    )


@bp.get("/<user_id>")
@require_role("librarian", "admin")
def get_user(user_id: str):
    doc = collection(USERS).find_one({"_id": to_object_id(user_id, "user id")}, PUBLIC)
    if doc is None:
        raise ApiError(404, "user not found")
    return jsonify(doc_out(doc))


@bp.get("/<user_id>/history")
@require_auth
def user_history(user_id: str):
    """A user's borrow history - one query, no $lookup.

    Possible only because borrow_records embeds a snapshot of the book
    (Extended Reference Pattern). Compare with /analytics/most_borrowed,
    which does need $lookup because it reads present-day stock levels.
    """
    oid = to_object_id(user_id, "user id")
    if collection(USERS).count_documents({"_id": oid}, limit=1) == 0:
        raise ApiError(404, "user not found")

    query: dict = {"user_id": oid}
    status = request.args.get("status")
    if status:
        query["status"] = status

    cursor = collection(BORROW_RECORDS).find(query).sort([("borrowed_at", DESCENDING)])
    records = [doc_out(doc) for doc in cursor]

    return jsonify(
        {
            "user_id": user_id,
            "total": len(records),
            "note": "no $lookup used - book title/authors come from the embedded snapshot",
            "items": records,
        }
    )


@bp.post("")
@require_role("librarian", "admin")
def create_user():
    doc = _user_payload(partial=False)
    doc["joined_at"] = datetime.now(timezone.utc)

    # A duplicate email trips the unique index and becomes a 409 via the
    # global DuplicateKeyError handler.
    result = collection(USERS).insert_one(doc)
    created = collection(USERS).find_one({"_id": result.inserted_id}, PUBLIC)
    return jsonify(doc_out(created)), 201


@bp.patch("/<user_id>")
@require_role("admin")
def update_user(user_id: str):
    changes = _user_payload(partial=True)

    doc = collection(USERS).find_one_and_update(
        {"_id": to_object_id(user_id, "user id")},
        {"$set": changes},
        projection=PUBLIC,
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise ApiError(404, "user not found")
    return jsonify(doc_out(doc))


@bp.delete("/<user_id>")
@require_role("admin")
def delete_user(user_id: str):
    oid = to_object_id(user_id, "user id")

    # Refuse to orphan an open loan.
    open_loans = collection(BORROW_RECORDS).count_documents(
        {"user_id": oid, "status": "borrowed"}
    )
    if open_loans:
        raise ApiError(409, "user still has books on loan", f"{open_loans} open loan(s)")

    result = collection(USERS).delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise ApiError(404, "user not found")
    return "", 204
