"""Books CRUD and querying (Module 2: CRUD, projections, filtering, sorting).

A Flask Blueprint. Route order does not matter here: Werkzeug scores a
static rule like /books/explain above a dynamic rule like /books/<book_id>,
so the two never collide.
"""

from datetime import datetime, timezone
from decimal import Decimal

from bson import Decimal128
from flask import Blueprint, jsonify, request
from pymongo import ASCENDING, DESCENDING, ReturnDocument

from app.core.auth import require_role
from app.core.errors import ApiError
from app.core.schema import GENRES
from app.core.serializers import doc_out, to_object_id
from app.core.validation import (
    MISSING,
    arg_bool,
    arg_int,
    field_decimal,
    field_enum,
    field_int,
    field_object,
    field_str,
    field_str_list,
    reject_unknown,
    require_body,
)
from app.db import BOOKS, BORROW_RECORDS, collection

bp = Blueprint("books", __name__, url_prefix="/books")

SORTABLE = {
    "title": "title",
    "year": "publication_year",
    "price": "price",
    "rating": "avg_rating",
    "popularity": "borrow_count",
    "added": "added_at",
}

BOOK_FIELDS = {
    "title",
    "authors",
    "genre",
    "publication_year",
    "price",
    "publisher",
    "copies",
    "tags",
}


# --------------------------------------------------------------------------
# payload parsing
# --------------------------------------------------------------------------


def _book_payload(partial: bool) -> dict:
    """Validate a request body into a storage-ready document fragment.

    partial=True drives PATCH: every field becomes optional, and only the
    fields the client actually supplied end up in the result.
    """
    data = require_body(request.get_json(silent=True))
    reject_unknown(data, BOOK_FIELDS)

    required = not partial
    out: dict = {}

    def keep(key, value):
        if value is not MISSING:
            out[key] = value

    keep("title", field_str(data, "title", required=required, max_len=300))
    keep("authors", field_str_list(data, "authors", required=required, min_items=1))
    keep("genre", field_enum(data, "genre", GENRES, required=required))
    keep(
        "publication_year",
        field_int(data, "publication_year", required=required, minimum=1450, maximum=2100),
    )

    price = field_decimal(data, "price", required=required, minimum=Decimal("0"))
    if price is not MISSING:
        # Money is stored as BSON Decimal128, never as a float.
        out["price"] = Decimal128(price)

    publisher = field_object(data, "publisher", required=required)
    if publisher is not MISSING:
        # Embedded document - always read together with the book.
        embedded = {"name": field_str(publisher, "name", max_len=200)}
        country = field_str(publisher, "country", required=False, max_len=100)
        if country is not MISSING:
            embedded["country"] = country
        out["publisher"] = embedded

    copies = field_object(data, "copies", required=required)
    if copies is not MISSING:
        total = field_int(copies, "total", minimum=0)
        available = field_int(copies, "available", minimum=0)
        if available > total:
            raise ApiError(
                400, "validation failed", "copies.available cannot exceed copies.total"
            )
        out["copies"] = {"total": total, "available": available}

    tags = field_str_list(data, "tags", required=False)
    if tags is not MISSING:
        out["tags"] = tags
    elif not partial:
        out["tags"] = []

    if partial and not out:
        raise ApiError(400, "no fields supplied")

    return out


# --------------------------------------------------------------------------
# query building
# --------------------------------------------------------------------------


def _build_filter() -> dict:
    """Translate the query string into a MongoDB filter document."""
    args = request.args
    query: dict = {}

    genre = args.get("genre")
    if genre:
        query["genre"] = genre

    author = args.get("author")
    if author:
        # authors is an array; a match on the field matches any element.
        query["authors"] = {"$regex": author, "$options": "i"}

    year_from = arg_int(args, "year_from")
    year_to = arg_int(args, "year_to")
    if year_from is not None or year_to is not None:
        year_range: dict = {}
        if year_from is not None:
            year_range["$gte"] = year_from
        if year_to is not None:
            year_range["$lte"] = year_to
        query["publication_year"] = year_range

    tag = args.get("tag")
    if tag:
        query["tags"] = tag

    publisher_country = args.get("publisher_country")
    if publisher_country:
        # Dot notation into the embedded publisher document.
        query["publisher.country"] = publisher_country

    if arg_bool(args, "available_only"):
        query["copies.available"] = {"$gt": 0}

    q = args.get("q")
    if q:
        query["$text"] = {"$search": q}

    return query


def _build_projection() -> dict | None:
    fields = request.args.get("fields")
    if not fields:
        return None
    wanted = [f.strip() for f in fields.split(",") if f.strip()]
    if not wanted:
        return None
    return {field: 1 for field in wanted}


def _build_sort() -> list[tuple[str, int]]:
    key, _, direction = request.args.get("sort", "added:desc").partition(":")
    field = SORTABLE.get(key)
    if field is None:
        raise ApiError(400, f"sort key must be one of {sorted(SORTABLE)}")
    return [(field, DESCENDING if direction == "desc" else ASCENDING)]


def _popularity_pipeline(query: dict, skip: int, limit: int) -> list:
    """Rank by loans counted from borrow_records, not by the stored field.

    Every other sort on this page is an ordinary indexed find().sort() over a
    field that already exists on the book document. "Most borrowed" is the
    exception: the ranking is DERIVED here, by joining each matching book to
    its loan records and counting them at query time.

    books.borrow_count holds the same number as a Computed Pattern cache. The
    response returns both, so the two can be compared side by side: the
    pipeline proves the cached value is correct.

    $match runs first so the join only touches books that already passed the
    filters, and so a $text search (which must be the first stage) still works.
    """
    return [
        {"$match": query},
        {
            "$lookup": {
                "from": BORROW_RECORDS,
                "localField": "_id",
                "foreignField": "book.book_id",
                "as": "loans",
            }
        },
        {"$addFields": {"loans_counted": {"$size": "$loans"}}},
        # Drop the joined array before it reaches the client; only the count
        # is wanted, and the documents themselves would be large.
        {"$project": {"loans": 0}},
        {"$sort": {"loans_counted": DESCENDING, "_id": ASCENDING}},
        {"$skip": skip},
        {"$limit": limit},
    ]


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------


@bp.get("/explain")
def explain_query():
    """executionStats for the same filter GET /books would run.

    Shows IXSCAN vs COLLSCAN, documents examined, keys examined and
    milliseconds - the raw evidence for the indexing section of the report.
    """
    query = _build_filter()
    plan = collection(BOOKS).find(query).explain()
    execution = plan.get("executionStats", {})
    return jsonify(
        {
            "filter": query,
            "winning_plan": plan.get("queryPlanner", {}).get("winningPlan"),
            "docs_examined": execution.get("totalDocsExamined"),
            "keys_examined": execution.get("totalKeysExamined"),
            "docs_returned": execution.get("nReturned"),
            "millis": execution.get("executionTimeMillis"),
        }
    )


@bp.get("")
def list_books():
    query = _build_filter()
    limit = arg_int(request.args, "limit", default=20, minimum=1, maximum=100)
    skip = arg_int(request.args, "skip", default=0, minimum=0)

    sort_key = request.args.get("sort", "added:desc").partition(":")[0]

    body = {
        "total": collection(BOOKS).count_documents(query),
        "skip": skip,
        "limit": limit,
    }

    if sort_key == "popularity":
        # Derived ranking: the count does not exist on the book, so it has to
        # be computed across collections. That needs an aggregation pipeline.
        pipeline = _popularity_pipeline(query, skip, limit)
        body["items"] = [doc_out(doc) for doc in collection(BOOKS).aggregate(pipeline)]
        body["query_type"] = "aggregation pipeline"
        body["why"] = (
            "Loans are counted from borrow_records at query time, so the "
            "ranking cannot come from a single collection."
        )
        body["pipeline"] = pipeline
        body["stages"] = [stage for doc in pipeline for stage in doc]
    else:
        # Everything else sorts a field already stored on the book document,
        # which an index can serve directly. No pipeline is needed.
        cursor = (
            collection(BOOKS)
            .find(query, _build_projection())
            .sort(_build_sort())
            .skip(skip)
            .limit(limit)
        )
        body["items"] = [doc_out(doc) for doc in cursor]
        body["query_type"] = "standard query"
        body["why"] = (
            "find(filter).sort().skip().limit() over fields stored on the book "
            "document, served by an index."
        )

    return jsonify(body)


@bp.get("/<book_id>")
def get_book(book_id: str):
    doc = collection(BOOKS).find_one({"_id": to_object_id(book_id, "book id")})
    if doc is None:
        raise ApiError(404, "book not found")
    return jsonify(doc_out(doc))


@bp.post("")
@require_role("librarian", "admin")
def create_book():
    doc = _book_payload(partial=False)

    # Computed Pattern fields start at their zero values and are maintained
    # by the borrow/return transactions, never recalculated on read.
    doc.update(
        {
            "avg_rating": None,
            "borrow_count": 0,
            "review_count": 0,
            "has_many_reviews": False,
            "added_at": datetime.now(timezone.utc),
        }
    )

    # A $jsonSchema violation raises OperationFailure(121), which the global
    # handler in app/core/errors.py turns into a 422.
    result = collection(BOOKS).insert_one(doc)
    created = collection(BOOKS).find_one({"_id": result.inserted_id})
    return jsonify(doc_out(created)), 201


@bp.patch("/<book_id>")
@require_role("librarian", "admin")
def update_book(book_id: str):
    changes = _book_payload(partial=True)

    doc = collection(BOOKS).find_one_and_update(
        {"_id": to_object_id(book_id, "book id")},
        {"$set": changes},
        return_document=ReturnDocument.AFTER,
    )
    if doc is None:
        raise ApiError(404, "book not found")
    return jsonify(doc_out(doc))


@bp.delete("/<book_id>")
@require_role("librarian", "admin")
def delete_book(book_id: str):
    result = collection(BOOKS).delete_one({"_id": to_object_id(book_id, "book id")})
    if result.deleted_count == 0:
        raise ApiError(404, "book not found")
    return "", 204
