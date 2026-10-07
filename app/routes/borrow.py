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

from app.config import get_settings
from app.core.auth import require_auth
from app.core.errors import ApiError

# Imported under another name because _do_borrow() and return_book() use a
# local variable called `record` for the loan document.
from app.core.mongoshell import call, find_call
from app.core.mongoshell import record as log_op
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


def _log_transaction_start() -> None:
    name = get_settings().db_name
    log_op(
        "Open a transaction. Every write below runs inside it",
        "const session = db.getMongo().startSession()\n"
        f'const sdb = session.getDatabase("{name}")\n'
        "session.startTransaction()",
    )


def _log_abort(reason: str) -> None:
    log_op(reason, "session.abortTransaction()")


def _recompute_book_ratings(db, book_id, session) -> dict:
    """Computed Pattern maintenance.

    avg_rating and review_count are recalculated from borrow_records and
    STORED on the book, so the dashboard and the book list never have to
    aggregate ratings at read time.
    """
    rating_filter = {"book.book_id": book_id, "rating": {"$ne": None}}
    log_op(
        "Re-read this book's ratings so the stored average can be recomputed (Computed Pattern)",
        find_call(BORROW_RECORDS, rating_filter, {"rating": 1}, prefix="sdb"),
    )
    rated = list(
        db[BORROW_RECORDS].find(
            rating_filter,
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
        _log_transaction_start()
        with session.start_transaction():
            log_op(
                "Check the member exists",
                call(USERS, "findOne", {"_id": user_id}, {"name": 1}, prefix="sdb"),
            )
            user = db[USERS].find_one({"_id": user_id}, {"name": 1}, session=session)
            if user is None:
                _log_abort("The member was not found, so the transaction aborts")
                raise ApiError(404, "user not found")

            # Claim a copy in one atomic step. The filter requires
            # copies.available > 0, so two users racing for the last copy
            # cannot both succeed.
            claim_filter = {"_id": book_id, "copies.available": {"$gt": 0}}
            claim_update = {"$inc": {"copies.available": -1, "borrow_count": 1}}
            log_op(
                "Claim one copy in a single atomic step. The filter needs a copy to be "
                "available, so two people cannot take the last one",
                call(
                    BOOKS,
                    "findOneAndUpdate",
                    claim_filter,
                    claim_update,
                    {"returnDocument": "after"},
                    prefix="sdb",
                ),
            )
            book = db[BOOKS].find_one_and_update(
                claim_filter,
                claim_update,
                return_document=ReturnDocument.AFTER,
                session=session,
            )

            if book is None:
                log_op(
                    "No copy could be claimed. Find out why",
                    call(BOOKS, "countDocuments", {"_id": book_id}, {"limit": 1}, prefix="sdb"),
                )
                exists = db[BOOKS].count_documents({"_id": book_id}, limit=1, session=session)
                if not exists:
                    _log_abort("The book does not exist, so the transaction aborts")
                    raise ApiError(404, "book not found")
                _log_abort("No copy is available, so the transaction aborts")
                raise ApiError(409, "no copies available")

            if fail_midway:
                # The copy has been decremented but no record exists yet.
                # Raising here proves the decrement is undone on abort.
                _log_abort(
                    "DELIBERATE FAILURE before the loan is saved. The stock decrement above "
                    "was applied inside the transaction only. Aborting undoes it, so neither write survives"
                )
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
            log_op(
                "Save the loan. It keeps a snapshot of the title and authors (Extended Reference Pattern)",
                call(BORROW_RECORDS, "insertOne", record, prefix="sdb"),
            )
            result = db[BORROW_RECORDS].insert_one(record, session=session)

        # Committed once the inner block exits without raising.
        log_op(
            "Commit. The stock change and the loan become visible together",
            "session.commitTransaction()",
        )

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

    log_op(
        "Before: read the stock outside any transaction",
        call(BOOKS, "findOne", {"_id": book_id}, {"copies": 1}),
    )
    before = collection(BOOKS).find_one({"_id": book_id}, {"copies": 1})
    if before is None:
        raise ApiError(404, "book not found")

    log_op(
        "Before: count this book's loans",
        call(BORROW_RECORDS, "countDocuments", {"book.book_id": book_id}),
    )
    records_before = collection(BORROW_RECORDS).count_documents({"book.book_id": book_id})

    try:
        _do_borrow(user_id, book_id, days, fail_midway=True)
    except _DeliberateFailure:
        pass

    log_op(
        "After the abort: read the stock again. It should equal the value before",
        call(BOOKS, "findOne", {"_id": book_id}, {"copies": 1}),
    )
    after = collection(BOOKS).find_one({"_id": book_id}, {"copies": 1})
    log_op(
        "After the abort: count the loans again. The total should be unchanged",
        call(BORROW_RECORDS, "countDocuments", {"book.book_id": book_id}),
    )
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
        _log_transaction_start()
        with session.start_transaction():
            close_filter = {"_id": borrow_id, "status": "borrowed"}
            close_update = {
                "$set": {
                    "returned_at": datetime.now(timezone.utc),
                    "status": "returned",
                    "rating": rating,
                }
            }
            log_op(
                "Close the loan. The filter requires it to still be open, so a loan cannot be returned twice",
                call(
                    BORROW_RECORDS,
                    "findOneAndUpdate",
                    close_filter,
                    close_update,
                    {"returnDocument": "after"},
                    prefix="sdb",
                ),
            )
            record = db[BORROW_RECORDS].find_one_and_update(
                close_filter,
                close_update,
                return_document=ReturnDocument.AFTER,
                session=session,
            )

            if record is None:
                log_op(
                    "The loan was not open. Find out why",
                    call(BORROW_RECORDS, "countDocuments", {"_id": borrow_id}, {"limit": 1}, prefix="sdb"),
                )
                exists = db[BORROW_RECORDS].count_documents(
                    {"_id": borrow_id}, limit=1, session=session
                )
                if not exists:
                    _log_abort("The loan does not exist, so the transaction aborts")
                    raise ApiError(404, "borrow record not found")
                _log_abort("The loan was already returned, so the transaction aborts")
                raise ApiError(409, "this book was already returned")

            book_id = record["book"]["book_id"]

            # Put the copy back, then refresh the stored Computed Pattern
            # fields - all still inside the same transaction.
            log_op(
                "Put the copy back on the shelf",
                call(
                    BOOKS,
                    "updateOne",
                    {"_id": book_id},
                    {"$inc": {"copies.available": 1}},
                    prefix="sdb",
                ),
            )
            db[BOOKS].update_one(
                {"_id": book_id},
                {"$inc": {"copies.available": 1}},
                session=session,
            )

            refreshed = _recompute_book_ratings(db, book_id, session)
            log_op(
                "Store the recomputed average, rating count and outlier flag on the book (Computed Pattern)",
                call(BOOKS, "updateOne", {"_id": book_id}, {"$set": refreshed}, prefix="sdb"),
            )
            db[BOOKS].update_one(
                {"_id": book_id},
                {"$set": refreshed},
                session=session,
            )

        log_op(
            "Commit. The loan, the stock and the stored rating change together",
            "session.commitTransaction()",
        )

    log_op(
        "Read the book back for the response",
        call(
            BOOKS,
            "findOne",
            {"_id": book_id},
            {"title": 1, "copies": 1, "avg_rating": 1, "review_count": 1},
        ),
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

    log_op("Count every match, for the pager", call(BORROW_RECORDS, "countDocuments", query))
    log_op(
        "Find this page of loans, newest first",
        find_call(BORROW_RECORDS, query, None, [("borrowed_at", DESCENDING)], skip, limit),
    )
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
