"""Borrow and return - multi-document ACID transactions.

Borrowing touches two collections at once:

    books.copies.available  -1      (and borrow_count +1)
    borrow_records          insert

If only the first succeeded, the library would have lost a copy with no
record of who took it. Both writes therefore run inside one transaction, so
they either both commit or both roll back. Transactions require a replica
set, which is why Atlas M0 (a 3-node replica set) is used rather than a
standalone mongod.
"""

from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from pymongo import DESCENDING, ReturnDocument

from app.core.auth import require_auth
from app.core.errors import ApiError
from app.core.schema import OUTLIER_REVIEW_THRESHOLD
from app.core.serializers import doc_out, to_object_id
from app.core.validation import (
    MISSING,
    arg_int,
    field_int,
    field_str,
    reject_unknown,
    require_body,
)
from app.db import BOOKS, BORROW_RECORDS, USERS, collection, get_client, get_db

bp = Blueprint("borrow", __name__, url_prefix="/borrow")

DEFAULT_LOAN_DAYS = 14


class _DeliberateFailure(Exception):
    """Raised only by /borrow/demo-failure, to force a rollback."""


def _recompute_book_ratings(db, book_id, session) -> dict:
    """Computed Pattern maintenance.

    avg_rating and review_count are recalculated from borrow_records and
    STORED on the book, so the dashboard and the book list never have to
    aggregate ratings at read time.
    """
    rated = list(
        db[BORROW_RECORDS].find(
            {"book.book_id": book_id, "rating": {"$ne": None}},
            {"rating": 1},
            session=session,
        )
    )

    count = len(rated)
    average = round(sum(r["rating"] for r in rated) / count, 2) if count else None

    return {
        "avg_rating": average,
        "review_count": count,
        # Outlier Pattern flag: once a book collects this many reviews, its
        # review bodies live in reviews_overflow instead of on the book.
        "has_many_reviews": count > OUTLIER_REVIEW_THRESHOLD,
    }


def _load_borrow_request() -> tuple:
    data = require_body(request.get_json(silent=True))
    reject_unknown(data, {"user_id", "book_id", "days"})

    user_id = to_object_id(field_str(data, "user_id"), "user id")
    book_id = to_object_id(field_str(data, "book_id"), "book id")

    days = field_int(data, "days", required=False, minimum=1, maximum=90)
    if days is MISSING:
        days = DEFAULT_LOAN_DAYS

    return user_id, book_id, days


def _do_borrow(user_id, book_id, days: int, *, fail_midway: bool) -> dict:
    client = get_client()
    db = get_db()

    with client.start_session() as session:
        # This context manager commits on a clean exit and aborts on ANY
        # exception leaving the block - that is the whole rollback mechanism.
        with session.start_transaction():
            user = db[USERS].find_one({"_id": user_id}, {"name": 1}, session=session)
            if user is None:
                raise ApiError(404, "user not found")

            # Claim a copy in one atomic step. The filter requires
            # copies.available > 0, so two users racing for the last copy
            # cannot both succeed.
            book = db[BOOKS].find_one_and_update(
                {"_id": book_id, "copies.available": {"$gt": 0}},
                {"$inc": {"copies.available": -1, "borrow_count": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )

            if book is None:
                exists = db[BOOKS].count_documents({"_id": book_id}, limit=1, session=session)
                if not exists:
                    raise ApiError(404, "book not found")
                raise ApiError(409, "no copies available")

            if fail_midway:
                # The copy has been decremented but no record exists yet.
                # Raising here proves the decrement is undone on abort.
                raise _DeliberateFailure()

            borrowed_at = datetime.now(timezone.utc)
            record = {
                "user_id": user_id,
                # Extended Reference Pattern: a snapshot of the book as it
                # was at borrow time. Deliberately not kept in sync.
                "book": {
                    "book_id": book["_id"],
                    "title": book["title"],
                    "authors": book["authors"],
                },
                "borrowed_at": borrowed_at,
                "due_date": borrowed_at + timedelta(days=days),
                "returned_at": None,
                "status": "borrowed",
                "rating": None,
            }
            result = db[BORROW_RECORDS].insert_one(record, session=session)

        # Committed once the inner block exits without raising.

    return {
        "borrow_id": str(result.inserted_id),
        "book": {
            "id": str(book["_id"]),
            "title": book["title"],
            "available_after": book["copies"]["available"],
        },
        "due_date": record["due_date"].isoformat(),
    }


@bp.post("")
@require_auth
def borrow_book():
    user_id, book_id, days = _load_borrow_request()
    return jsonify(_do_borrow(user_id, book_id, days, fail_midway=False)), 201


@bp.post("/demo-failure")
@require_auth
def borrow_demo_failure():
    """Prove the rollback is real, not just described in the report.

    Decrements copies.available, then raises before the borrow_record is
    inserted. The response reports copies.available before and after; they
    must match, because the transaction aborted.
    """
    user_id, book_id, days = _load_borrow_request()

    before = collection(BOOKS).find_one({"_id": book_id}, {"copies": 1})
    if before is None:
        raise ApiError(404, "book not found")

    records_before = collection(BORROW_RECORDS).count_documents({"book.book_id": book_id})

    try:
        _do_borrow(user_id, book_id, days, fail_midway=True)
    except _DeliberateFailure:
        pass

    after = collection(BOOKS).find_one({"_id": book_id}, {"copies": 1})
    records_after = collection(BORROW_RECORDS).count_documents({"book.book_id": book_id})

    rolled_back = (
        before["copies"]["available"] == after["copies"]["available"]
        and records_before == records_after
    )

    return jsonify(
        {
            "what_happened": (
                "Inside one transaction: copies.available was decremented, then an "
                "exception was raised before the borrow_record insert. MongoDB "
                "aborted the transaction, so neither write survives."
            ),
            "copies_available_before": before["copies"]["available"],
            "copies_available_after": after["copies"]["available"],
            "borrow_records_before": records_before,
            "borrow_records_after": records_after,
            "rolled_back": rolled_back,
        }
    )


@bp.post("/return")
@require_auth
def return_book():
    data = require_body(request.get_json(silent=True))
    reject_unknown(data, {"borrow_id", "rating"})

    borrow_id = to_object_id(field_str(data, "borrow_id"), "borrow id")
    rating = field_int(data, "rating", required=False, minimum=1, maximum=5)
    if rating is MISSING:
        rating = None

    client = get_client()
    db = get_db()

    with client.start_session() as session:
        with session.start_transaction():
            record = db[BORROW_RECORDS].find_one_and_update(
                {"_id": borrow_id, "status": "borrowed"},
                {
                    "$set": {
                        "returned_at": datetime.now(timezone.utc),
                        "status": "returned",
                        "rating": rating,
                    }
                },
                return_document=ReturnDocument.AFTER,
                session=session,
            )

            if record is None:
                exists = db[BORROW_RECORDS].count_documents(
                    {"_id": borrow_id}, limit=1, session=session
                )
                if not exists:
                    raise ApiError(404, "borrow record not found")
                raise ApiError(409, "this book was already returned")

            book_id = record["book"]["book_id"]

            # Put the copy back, then refresh the stored Computed Pattern
            # fields - all still inside the same transaction.
            db[BOOKS].update_one(
                {"_id": book_id},
                {"$inc": {"copies.available": 1}},
                session=session,
            )
            db[BOOKS].update_one(
                {"_id": book_id},
                {"$set": _recompute_book_ratings(db, book_id, session)},
                session=session,
            )

    book = collection(BOOKS).find_one(
        {"_id": book_id},
        {"title": 1, "copies": 1, "avg_rating": 1, "review_count": 1},
    )

    return jsonify(
        {
            "borrow_id": str(borrow_id),
            "status": "returned",
            "rating_given": rating,
            "book": doc_out(book),
        }
    )


@bp.get("")
@require_auth
def list_borrows():
    query: dict = {}

    status = request.args.get("status")
    if status:
        query["status"] = status

    user_id = request.args.get("user_id")
    if user_id:
        query["user_id"] = to_object_id(user_id, "user id")

    book_id = request.args.get("book_id")
    if book_id:
        query["book.book_id"] = to_object_id(book_id, "book id")

    if request.args.get("overdue") == "true":
        # Served by the partial index on due_date, which only indexes
        # records whose status is still "borrowed".
        query["status"] = "borrowed"
        query["due_date"] = {"$lt": datetime.now(timezone.utc)}

    limit = arg_int(request.args, "limit", default=20, minimum=1, maximum=100)
    skip = arg_int(request.args, "skip", default=0, minimum=0)

    cursor = (
        collection(BORROW_RECORDS)
        .find(query)
        .sort([("borrowed_at", DESCENDING)])
        .skip(skip)
        .limit(limit)
    )

    return jsonify(
        {
            "total": collection(BORROW_RECORDS).count_documents(query),
            "skip": skip,
            "limit": limit,
            "items": [doc_out(doc) for doc in cursor],
        }
    )
