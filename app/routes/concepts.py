"""Concepts tab: every MongoDB idea in the project, demonstrated live.

Each endpoint here runs a real operation against the real database and sends
the evidence back, so the idea can be shown on screen without opening code:

  /concepts/computed/<book>             stored value vs value recomputed now
  /concepts/computed/<book>/simulate    5-star return inside a transaction that
                                        is ABORTED, so nothing is saved
  /concepts/extended-reference/<loan>   one findOne vs $lookup, snapshot drift
  /concepts/outliers                    document sizes measured by $bsonSize
  /concepts/types/<book>                BSON type of every field ($type)
  /concepts/index-lab                   planner choice vs forced full scan
  /concepts/validation                  invalid inserts rejected by MongoDB
  /concepts/replica-set                 primary / secondaries / topology

Only two demos write, and both leave nothing behind: the simulation aborts
its transaction, and the validation control document is deleted again.
"""

from datetime import datetime, timezone

from bson import Decimal128, ObjectId
from flask import Blueprint, g, jsonify, request
from pymongo.errors import OperationFailure

from app.core.auth import require_auth
from app.core.errors import DOCUMENT_VALIDATION_FAILURE, ApiError
from app.core.mongoshell import agg_call, call, find_call, record, shell
from app.core.schema import BOOK_VALIDATOR, GENRES, OUTLIER_REVIEW_THRESHOLD
from app.core.serializers import doc_out, jsonable, to_object_id
from app.db import BOOKS, BORROW_RECORDS, REVIEWS_OVERFLOW, collection, get_client, get_db
from app.routes.borrow import _log_abort, _log_transaction_start, _recompute_book_ratings

bp = Blueprint("concepts", __name__, url_prefix="/concepts")

DOCUMENT_LIMIT_BYTES = 16 * 1024 * 1024

_STORED = {"title": 1, "authors": 1, "avg_rating": 1, "review_count": 1, "borrow_count": 1,
           "has_many_reviews": 1, "copies": 1}


def _stored(book: dict) -> dict:
    return {
        "avg_rating": book.get("avg_rating"),
        "review_count": book.get("review_count"),
        "borrow_count": book.get("borrow_count"),
    }


def _load_book(book_id: str, projection=None) -> tuple:
    oid = to_object_id(book_id, "book id")
    args = (BOOKS, "findOne", {"_id": oid}) + ((projection,) if projection else ())
    record("Read the book", call(*args))
    book = collection(BOOKS).find_one({"_id": oid}, projection)
    if book is None:
        raise ApiError(404, "book not found")
    return oid, book


# ---------------------------------------------------------------- Computed

@bp.get("/computed/<book_id>")
def computed(book_id: str):
    """Stored (Computed Pattern) values next to the same numbers recomputed now."""
    oid, book = _load_book(book_id, _STORED)

    pipeline = [
        {"$match": {"book.book_id": oid}},
        {
            "$group": {
                "_id": None,
                "loans": {"$sum": 1},
                "rated": {"$sum": {"$cond": [{"$ne": ["$rating", None]}, 1, 0]}},
                "avg": {"$avg": "$rating"},
            }
        },
    ]
    record(
        "Recompute the same numbers from every loan right now. The stored values avoid doing this on each page view",
        agg_call(BORROW_RECORDS, pipeline),
    )
    rows = list(collection(BORROW_RECORDS).aggregate(pipeline))
    live_row = rows[0] if rows else {"loans": 0, "rated": 0, "avg": None}
    live = {
        "avg_rating": round(live_row["avg"], 2) if live_row["avg"] is not None else None,
        "review_count": live_row["rated"],
        "borrow_count": live_row["loans"],
    }
    stored = _stored(book)
    matches = {key: stored[key] == live[key] for key in stored}

    note = (
        "This is an outlier book: part of its reviews live in reviews_overflow, "
        "so the live figure here only covers loans."
        if book.get("has_many_reviews")
        else None
    )
    return jsonify(
        {
            "book": {"id": book_id, "title": book["title"]},
            "stored": stored,
            "live": live,
            "matches": matches,
            "note": note,
        }
    )


@bp.post("/computed/<book_id>/simulate")
@require_auth
def simulate_five_star(book_id: str):
    """Return a book with a 5-star rating inside a transaction, then ABORT it.

    Shows how the stored values change (inside the transaction) and that the
    abort puts everything back (after).
    """
    oid = to_object_id(book_id, "book id")
    db = get_db()
    projection = {**_STORED, "title": 1, "authors": 1}

    record("Read the book before the simulation", call(BOOKS, "findOne", {"_id": oid}, projection))
    book = db[BOOKS].find_one({"_id": oid}, projection)
    if book is None:
        raise ApiError(404, "book not found")
    before = _stored(book)

    now = datetime.now(timezone.utc)
    loan = {
        "user_id": ObjectId(g.user["sub"]),
        "book": {"book_id": oid, "title": book["title"], "authors": book["authors"]},
        "borrowed_at": now,
        "due_date": now,
        "returned_at": now,
        "status": "returned",
        "rating": 5,
    }

    with get_client().start_session() as session:
        _log_transaction_start()
        session.start_transaction()
        try:
            record(
                "Save a temporary 5-star loan (inside the transaction only)",
                call(BORROW_RECORDS, "insertOne", loan, prefix="sdb"),
            )
            db[BORROW_RECORDS].insert_one(loan, session=session)

            computed_values = _recompute_book_ratings(db, oid, session)
            update = {
                "$inc": {"borrow_count": 1},
                "$set": {
                    "avg_rating": computed_values["avg_rating"],
                    "review_count": computed_values["review_count"],
                },
            }
            record(
                "Store the recomputed average and counts on the book (Computed Pattern write)",
                call(BOOKS, "updateOne", {"_id": oid}, update, prefix="sdb"),
            )
            db[BOOKS].update_one({"_id": oid}, update, session=session)

            record(
                "Read the book again, still inside the transaction",
                call(BOOKS, "findOne", {"_id": oid}, projection, prefix="sdb"),
            )
            inside_doc = db[BOOKS].find_one({"_id": oid}, projection, session=session)
        finally:
            if session.in_transaction:
                _log_abort(
                    "ABORT. Nothing from this simulation is saved: not the loan, "
                    "not the new average, not the borrow count"
                )
                session.abort_transaction()

    record("Read the book once more, after the abort", call(BOOKS, "findOne", {"_id": oid}, projection))
    after_doc = db[BOOKS].find_one({"_id": oid}, projection)
    loan_survived = db[BORROW_RECORDS].count_documents({"_id": loan["_id"]}) > 0

    after = _stored(after_doc)
    return jsonify(
        {
            "book": {"id": book_id, "title": book["title"]},
            "before": before,
            "inside_transaction": _stored(inside_doc),
            "after_abort": after,
            "rolled_back": after == before and not loan_survived,
            "temporary_loan_exists_after": loan_survived,
        }
    )


# ------------------------------------------------------ Extended Reference

@bp.get("/sample-loans")
def sample_loans():
    query: dict = {}
    projection = {"book": 1, "status": 1, "borrowed_at": 1}
    record(
        "Pick some recent loans to demonstrate with",
        find_call(BORROW_RECORDS, query, projection, [("borrowed_at", -1)], None, 12),
    )
    cursor = (
        collection(BORROW_RECORDS)
        .find(query, projection)
        .sort([("borrowed_at", -1)])
        .limit(12)
    )
    return jsonify({"items": [doc_out(doc) for doc in cursor]})


@bp.get("/extended-reference/<borrow_id>")
def extended_reference(borrow_id: str):
    """The same screen with and without a $lookup, plus snapshot drift."""
    oid = to_object_id(borrow_id, "loan id")

    record("WITHOUT a join: one read is enough, the title is inside the loan", call(BORROW_RECORDS, "findOne", {"_id": oid}))
    loan = collection(BORROW_RECORDS).find_one({"_id": oid})
    if loan is None:
        raise ApiError(404, "loan not found")

    pipeline = [
        {"$match": {"_id": oid}},
        {
            "$lookup": {
                "from": BOOKS,
                "localField": "book.book_id",
                "foreignField": "_id",
                "as": "current_book",
            }
        },
        {"$unwind": {"path": "$current_book", "preserveNullAndEmptyArrays": True}},
        {"$project": {"book": 1, "current_title": "$current_book.title"}},
    ]
    record("WITH a join: the same answer through $lookup, to compare the work needed", agg_call(BORROW_RECORDS, pipeline))
    joined = next(iter(collection(BORROW_RECORDS).aggregate(pipeline)), None)

    snapshot_title = loan["book"]["title"]
    current_title = joined.get("current_title") if joined else None
    return jsonify(
        {
            "loan": doc_out(loan),
            "snapshot_title": snapshot_title,
            "current_title": current_title,
            "book_still_exists": current_title is not None,
            "drifted": current_title is not None and current_title != snapshot_title,
        }
    )


# ----------------------------------------------------------------- Outlier

@bp.get("/outliers")
def outliers():
    """Measure real document sizes with $bsonSize."""
    book_pipeline = [
        {"$match": {"has_many_reviews": True}},
        {"$project": {"title": 1, "review_count": 1, "bytes": {"$bsonSize": "$$ROOT"}}},
        {"$sort": {"review_count": -1}},
        {"$limit": 10},
    ]
    bucket_pipeline = [
        {
            "$group": {
                "_id": "$book_id",
                "buckets": {"$sum": 1},
                "reviews": {"$sum": {"$size": "$reviews"}},
                "bytes": {"$sum": {"$bsonSize": "$$ROOT"}},
            }
        }
    ]
    average_pipeline = [
        {"$match": {"has_many_reviews": {"$ne": True}}},
        {"$group": {"_id": None, "average": {"$avg": {"$bsonSize": "$$ROOT"}}, "books": {"$sum": 1}}},
    ]

    record("Outlier books and the real size of each document, measured by MongoDB", agg_call(BOOKS, book_pipeline))
    outlier_books = list(collection(BOOKS).aggregate(book_pipeline))
    record("Size of the overflow buckets that hold those books' reviews", agg_call(REVIEWS_OVERFLOW, bucket_pipeline))
    buckets = {row["_id"]: row for row in collection(REVIEWS_OVERFLOW).aggregate(bucket_pipeline)}
    record("Average size of an ordinary book, for comparison", agg_call(BOOKS, average_pipeline))
    average_rows = list(collection(BOOKS).aggregate(average_pipeline))
    average = round(average_rows[0]["average"]) if average_rows else 0

    items = []
    for book in outlier_books:
        bucket = buckets.get(book["_id"], {"buckets": 0, "reviews": 0, "bytes": 0})
        embedded = book["bytes"] + bucket["bytes"]
        per_review = bucket["bytes"] / bucket["reviews"] if bucket["reviews"] else 0
        items.append(
            {
                "id": str(book["_id"]),
                "title": book["title"],
                "review_count": book.get("review_count"),
                "book_document_bytes": book["bytes"],
                "overflow_buckets": bucket["buckets"],
                "overflow_reviews": bucket["reviews"],
                "bucket_bytes": bucket["bytes"],
                "if_embedded_bytes": embedded,
                "times_larger": round(embedded / average, 1) if average else None,
                "reviews_to_hit_limit": (
                    int((DOCUMENT_LIMIT_BYTES - book["bytes"]) / per_review) if per_review else None
                ),
            }
        )

    return jsonify(
        {
            "threshold": OUTLIER_REVIEW_THRESHOLD,
            "average_book_bytes": average,
            "document_limit_bytes": DOCUMENT_LIMIT_BYTES,
            "items": items,
            "honest_note": "The overflow buckets were created by the seeder to represent popular books. "
            "The sizes are measured for real; the volume of reviews is generated.",
        }
    )


# ------------------------------------------------------------- BSON types

BOOK_FIELDS = [
    ("_id", "identity: ObjectId generated by MongoDB"),
    ("title", "plain scalar"),
    ("authors", "EMBEDDED array: always read with the book"),
    ("genre", "plain scalar, restricted by an enum"),
    ("publication_year", "int32, range-checked by the validator"),
    ("price", "Decimal128: money is never a float"),
    ("publisher.name", "EMBEDDED document (one-to-few)"),
    ("publisher.country", "EMBEDDED document (one-to-few)"),
    ("copies.total", "EMBEDDED document"),
    ("copies.available", "EMBEDDED document, changed atomically by $inc"),
    ("tags", "EMBEDDED array"),
    ("avg_rating", "COMPUTED pattern: stored, maintained on write"),
    ("borrow_count", "COMPUTED pattern: stored, maintained on write"),
    ("review_count", "COMPUTED pattern: stored, maintained on write"),
    ("has_many_reviews", "OUTLIER flag: true when reviews moved to reviews_overflow"),
    ("added_at", "a real BSON date, not a string"),
]

LOAN_FIELDS = [
    ("_id", "identity"),
    ("user_id", "REFERENCE to a users document (ObjectId)"),
    ("book.book_id", "REFERENCE to the books document (ObjectId)"),
    ("book.title", "EXTENDED REFERENCE: snapshot copied at borrow time"),
    ("book.authors", "EXTENDED REFERENCE: snapshot copied at borrow time"),
    ("borrowed_at", "a real BSON date"),
    ("due_date", "a real BSON date"),
    ("returned_at", "date, or null while the book is out"),
    ("status", "enum: borrowed / returned / overdue"),
    ("rating", "int32 or null"),
]


def _dig(document: dict, path: str):
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _sample(value) -> str:
    text = str(jsonable(value))
    return text if len(text) <= 60 else text[:57] + "..."


def _type_rows(collection_name: str, doc_id, document: dict, fields: list) -> list:
    projection = {path.replace(".", "__"): {"$type": f"${path}"} for path, _ in fields}
    pipeline = [{"$match": {"_id": doc_id}}, {"$project": {"_id": 0, **projection}}]
    record("Ask MongoDB for the BSON type of every field", agg_call(collection_name, pipeline))
    types = next(iter(collection(collection_name).aggregate(pipeline)), {})
    return [
        {
            "path": path,
            "role": role,
            "type": types.get(path.replace(".", "__"), "missing"),
            "sample": _sample(_dig(document, path)),
        }
        for path, role in fields
    ]


@bp.get("/types/<book_id>")
def types(book_id: str):
    """Embedding vs referencing, shown field by field with real BSON types."""
    oid, book = _load_book(book_id)
    result = {"book": {"id": book_id, "title": book["title"]}, "book_fields": _type_rows(BOOKS, oid, book, BOOK_FIELDS)}

    loan_query = {"book.book_id": oid}
    record("Find a loan that references this book", find_call(BORROW_RECORDS, loan_query, None, None, None, 1))
    loan = collection(BORROW_RECORDS).find_one(loan_query)
    result["loan_fields"] = _type_rows(BORROW_RECORDS, loan["_id"], loan, LOAN_FIELDS) if loan else None
    return jsonify(result)


# --------------------------------------------------------------- Index lab

def _plan_stages(plan: dict, stages=None, indexes=None):
    stages = [] if stages is None else stages
    indexes = [] if indexes is None else indexes
    if not isinstance(plan, dict):
        return stages, indexes
    plan = plan.get("queryPlan", plan)
    if "stage" in plan:
        stages.append(plan["stage"])
    if plan.get("indexName"):
        indexes.append(plan["indexName"])
    for child in ([plan["inputStage"]] if "inputStage" in plan else []) + plan.get("inputStages", []):
        _plan_stages(child, stages, indexes)
    return stages, indexes


def _summarise(explain: dict) -> dict:
    stages, indexes = _plan_stages(explain.get("queryPlanner", {}).get("winningPlan", {}))
    stats = explain.get("executionStats", {})
    return {
        "stages": stages,
        "index": indexes[0] if indexes else None,
        "uses_index": "IXSCAN" in stages or "TEXT_MATCH" in stages,
        "docs_examined": stats.get("totalDocsExamined"),
        "keys_examined": stats.get("totalKeysExamined"),
        "returned": stats.get("nReturned"),
        "millis": stats.get("executionTimeMillis"),
    }


def _presets(sample: dict) -> dict:
    # Longest word: short words such as "the" are stop words the text index skips.
    title_word = max(sample.get("title", "book").split(), key=len)
    return {
        "genre": ("Equality on genre", {"genre": sample["genre"]}, "Served by the genre_year compound index (its prefix)."),
        "genre_year": (
            "Genre + year range",
            {"genre": sample["genre"], "publication_year": {"$gte": sample["publication_year"]}},
            "Uses BOTH fields of genre_year: equality first, then range.",
        ),
        "author": ("Author (array field)", {"authors": sample["authors"][0]}, "authors_idx is a MULTIKEY index: one entry per array element."),
        "publisher_name": ("Publisher name (embedded field)", {"publisher.name": sample["publisher"]["name"]}, "Index on a dotted path into an embedded document."),
        "in_stock": (
            "In stock",
            {"copies.available": {"$gt": 0}},
            "available_partial only indexes books with a copy on the shelf. If most books qualify, the planner may still scan.",
        ),
        "text": ("Text search", {"$text": {"$search": title_word}}, "Needs book_text_search. A text query cannot run as a plain scan at all."),
        "publisher_country": (
            "Publisher country",
            {"publisher.country": sample.get("publisher", {}).get("country", "")},
            "No index exists for this field, so both runs scan. This is what a missing index looks like.",
        ),
        "tag": ("A tag", {"tags": (sample.get("tags") or ["x"])[0]}, "Tags are only inside the text index, which cannot serve an equality filter, so this scans."),
        "year": (
            "Year alone",
            {"publication_year": sample["publication_year"]},
            "genre_year starts with genre. A query without its prefix cannot use it, so this scans.",
        ),
    }


def _explain(filter_doc: dict, hint=None) -> dict:
    find: dict = {"find": BOOKS, "filter": filter_doc}
    if hint:
        find["hint"] = hint
    return get_db().command({"explain": find, "verbosity": "executionStats"})


@bp.get("/index-lab")
def index_lab():
    preset = request.args.get("preset", "genre")

    sample_query = {"authors.0": {"$exists": True}, "publisher.name": {"$exists": True}}
    record("Take one book to build realistic filters from", find_call(BOOKS, sample_query, None, [("_id", 1)], None, 1))
    sample = collection(BOOKS).find_one(sample_query, sort=[("_id", 1)])
    if sample is None:
        raise ApiError(404, "no books to demonstrate with")

    presets = _presets(sample)
    if preset not in presets:
        raise ApiError(400, f"unknown preset '{preset}'", f"choose one of {sorted(presets)}")
    label, filter_doc, why = presets[preset]

    record(
        "What the query planner chooses by itself",
        find_call(BOOKS, filter_doc, None, None, None, None, explain=True),
    )
    planner = _summarise(_explain(filter_doc))

    record(
        "The same query, forced to read the whole collection with hint({ $natural: 1 })",
        find_call(BOOKS, filter_doc, None, None, None, None, explain=True, hint={"$natural": 1}),
    )
    try:
        forced = _summarise(_explain(filter_doc, {"$natural": 1}))
        forced_error = None
    except OperationFailure as exc:
        forced, forced_error = None, str(exc)

    total = collection(BOOKS).estimated_document_count()
    return jsonify(
        {
            "preset": preset,
            "label": label,
            "filter": jsonable(filter_doc),
            "why": why,
            "collection_documents": total,
            "planner": planner,
            "forced_scan": forced,
            "forced_scan_error": forced_error,
            "presets": {key: value[0] for key, value in presets.items()},
        }
    )


# ------------------------------------------------------- Schema validation

def _valid_book() -> dict:
    return {
        "title": "[validation demo] control book",
        "authors": ["Demo Author"],
        "genre": GENRES[0],
        "publication_year": 2020,
        "price": Decimal128("9.99"),
        "publisher": {"name": "Demo Press", "country": "India"},
        "copies": {"total": 1, "available": 1},
        "tags": ["demo"],
        "added_at": datetime.now(timezone.utc),
    }


def _case_missing_title(doc):
    doc.pop("title")


def _case_title_number(doc):
    doc["title"] = 12345


def _case_bad_genre(doc):
    doc["genre"] = "Cooking"


def _case_price_float(doc):
    doc["price"] = 9.99


def _case_negative_copies(doc):
    doc["copies"]["available"] = -3


def _case_year_range(doc):
    doc["publication_year"] = 1200


CASES = {
    "missing_title": ("Required field missing", "No title", _case_missing_title),
    "title_number": ("Wrong type", "title is the number 12345, not a string", _case_title_number),
    "bad_genre": ("Value outside the enum", 'genre is "Cooking", which is not in the list', _case_bad_genre),
    "price_float": ("Money as a float", "price is a double. The schema demands Decimal128", _case_price_float),
    "negative_copies": ("Below the minimum", "copies.available is -3", _case_negative_copies),
    "year_range": ("Outside the range", "publication_year is 1200 (minimum 1450)", _case_year_range),
    "control": ("Valid control", "A correct document, to prove the database accepts good data. It is deleted straight away", None),
}


def _reasons(info, found=None) -> list:
    found = [] if found is None else found
    if isinstance(info, dict):
        if "propertyName" in info or "reason" in info or "missingProperties" in info:
            found.append(
                {
                    key: jsonable(info[key])
                    for key in ("propertyName", "operatorName", "reason", "specifiedAs", "consideredValue",
                                "consideredType", "missingProperties")
                    if key in info
                }
            )
        for value in info.values():
            _reasons(value, found)
    elif isinstance(info, list):
        for value in info:
            _reasons(value, found)
    return found


@bp.get("/validation")
def validation_cases():
    return jsonify(
        {
            "validator": jsonable(BOOK_VALIDATOR),
            "validator_shell": shell(BOOK_VALIDATOR),
            "cases": [{"key": key, "label": label, "detail": detail} for key, (label, detail, _) in CASES.items()],
        }
    )


@bp.post("/validation")
@require_auth
def validation_attempt():
    """Insert a bad document straight into MongoDB, bypassing every Flask check."""
    body = request.get_json(silent=True) or {}
    key = body.get("case")
    if key not in CASES:
        raise ApiError(400, "unknown case", f"choose one of {sorted(CASES)}")
    label, detail, mutate = CASES[key]

    doc = _valid_book()
    if mutate:
        mutate(doc)

    record(f"Insert straight into MongoDB ({label}). No Flask validation runs first", call(BOOKS, "insertOne", doc))
    try:
        result = collection(BOOKS).insert_one(doc)
    except OperationFailure as exc:
        if exc.code != DOCUMENT_VALIDATION_FAILURE:
            raise
        info = (exc.details or {}).get("errInfo")
        return jsonify(
            {
                "case": key,
                "label": label,
                "accepted": False,
                "error_code": exc.code,
                "message": "Document failed validation",
                "reasons": _reasons(info),
                "detail": jsonable(info),
            }
        )

    record("Clean up: delete the control document again", call(BOOKS, "deleteOne", {"_id": result.inserted_id}))
    collection(BOOKS).delete_one({"_id": result.inserted_id})
    return jsonify({"case": key, "label": label, "accepted": True, "error_code": None, "removed_again": True})


# ------------------------------------------------------------- Replica set

@bp.get("/replica-set")
def replica_set():
    admin = get_client().admin
    record("Ask the server about its replica set", "db.hello()")
    hello = admin.command("hello")

    members = None
    try:
        record("Detailed member status", "rs.status()")
        status = admin.command("replSetGetStatus")
        members = [
            {"name": m.get("name"), "state": m.get("stateStr"), "health": m.get("health")}
            for m in status.get("members", [])
        ]
    except OperationFailure:
        # Atlas shared tiers hide replSetGetStatus; hello still lists hosts.
        pass

    if members is None:
        primary = hello.get("primary")
        members = [
            {"name": host, "state": "PRIMARY" if host == primary else "SECONDARY", "health": 1}
            for host in hello.get("hosts", [])
        ]

    record("Server version", "db.version()")
    version = get_client().server_info().get("version")
    return jsonify(
        {
            "set_name": hello.get("setName"),
            "primary": hello.get("primary"),
            "connected_to": hello.get("me"),
            "is_primary": hello.get("isWritablePrimary"),
            "members": members,
            "version": version,
            "write_concern_note": "Writes go to the primary and replicate to the secondaries. "
            "If the primary fails, a secondary is elected and the driver reconnects by itself.",
        }
    )
