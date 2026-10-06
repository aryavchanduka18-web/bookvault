"""Aggregation pipelines (Module 3, sessions 16-18).

Every number on the dashboard is produced by MongoDB, not by Python. The
pipelines live in one dict so GET /analytics/<name> can run any single one
on its own - useful in a demo, where showing one pipeline beside its output
is clearer than dumping the whole dashboard.

Stage coverage:
  $match $project $group $sort $limit $unwind $lookup $facet $bucket
  $addFields $dateToString
"""

from flask import Blueprint, jsonify, request

from app.core.errors import ApiError
from app.core.serializers import jsonable
from app.db import BOOKS, BORROW_RECORDS, USERS, collection

bp = Blueprint("analytics", __name__, url_prefix="/analytics")


# --------------------------------------------------------------------------
# pipelines over `books`
# --------------------------------------------------------------------------

# price is Decimal128, so cast it to a double before bucketing to keep the
# boundary comparison inside one numeric type.
_PRICE_AS_NUMBER = {"$addFields": {"price_num": {"$toDouble": "$price"}}}


BY_GENRE = [
    {"$group": {
        "_id": "$genre",
        "books": {"$sum": 1},
        "copies": {"$sum": "$copies.total"},
        "avg_price": {"$avg": {"$toDouble": "$price"}},
    }},
    {"$sort": {"books": -1}},
    {"$project": {
        "_id": 0,
        "genre": "$_id",
        "books": 1,
        "copies": 1,
        "avg_price": {"$round": ["$avg_price", 2]},
    }},
]

PRICE_DISTRIBUTION = [
    _PRICE_AS_NUMBER,
    {"$bucket": {
        "groupBy": "$price_num",
        "boundaries": [0, 200, 500, 1000, 2000, 5000],
        "default": "5000+",
        "output": {"count": {"$sum": 1}, "avg_price": {"$avg": "$price_num"}},
    }},
    {"$project": {
        "_id": 0,
        "from_price": "$_id",
        "count": 1,
        "avg_price": {"$round": ["$avg_price", 2]},
    }},
]

TOP_RATED = [
    {"$match": {"avg_rating": {"$ne": None}, "review_count": {"$gte": 3}}},
    {"$sort": {"avg_rating": -1, "review_count": -1}},
    {"$limit": 10},
    {"$project": {"_id": 0, "title": 1, "authors": 1, "avg_rating": 1, "review_count": 1}},
]

BY_DECADE = [
    {"$group": {
        "_id": {"$multiply": [{"$floor": {"$divide": ["$publication_year", 10]}}, 10]},
        "books": {"$sum": 1},
    }},
    {"$sort": {"_id": 1}},
    {"$project": {"_id": 0, "decade": "$_id", "books": 1}},
]

TOP_PUBLISHERS = [
    {"$group": {"_id": "$publisher.name", "books": {"$sum": 1}}},
    {"$sort": {"books": -1}},
    {"$limit": 10},
    {"$project": {"_id": 0, "publisher": "$_id", "books": 1}},
]

POPULAR_TAGS = [
    # $unwind turns each tag in the array into its own document, so tags can
    # be counted across the whole collection.
    {"$unwind": "$tags"},
    {"$group": {"_id": "$tags", "books": {"$sum": 1}}},
    {"$sort": {"books": -1}},
    {"$limit": 15},
    {"$project": {"_id": 0, "tag": "$_id", "books": 1}},
]

TOTALS = [
    {"$group": {
        "_id": None,
        "titles": {"$sum": 1},
        "copies_total": {"$sum": "$copies.total"},
        "copies_available": {"$sum": "$copies.available"},
        "inventory_value": {
            "$sum": {"$multiply": [{"$toDouble": "$price"}, "$copies.total"]}
        },
    }},
    {"$project": {
        "_id": 0,
        "titles": 1,
        "copies_total": 1,
        "copies_available": 1,
        "copies_on_loan": {"$subtract": ["$copies_total", "$copies_available"]},
        "inventory_value": {"$round": ["$inventory_value", 2]},
    }},
]


# --------------------------------------------------------------------------
# pipelines over `borrow_records`
# --------------------------------------------------------------------------

# $lookup is needed here even though borrow_records embeds the book title,
# because this aggregates across users and then pulls the CURRENT book
# document to read live stock levels. The embedded snapshot is a historical
# record; the join gets present-day state. That contrast is the point.
MOST_BORROWED = [
    {"$group": {
        "_id": "$book.book_id",
        "title": {"$first": "$book.title"},
        "times_borrowed": {"$sum": 1},
    }},
    {"$sort": {"times_borrowed": -1}},
    {"$limit": 10},
    {"$lookup": {"from": BOOKS, "localField": "_id", "foreignField": "_id", "as": "book"}},
    {"$unwind": "$book"},
    {"$project": {
        "_id": 0,
        "book_id": "$_id",
        "title": 1,
        "times_borrowed": 1,
        "genre": "$book.genre",
        "copies_available": "$book.copies.available",
        "avg_rating": "$book.avg_rating",
    }},
]

TOP_USERS = [
    {"$group": {"_id": "$user_id", "books_borrowed": {"$sum": 1}}},
    {"$sort": {"books_borrowed": -1}},
    {"$limit": 10},
    {"$lookup": {"from": USERS, "localField": "_id", "foreignField": "_id", "as": "user"}},
    {"$unwind": "$user"},
    {"$project": {
        "_id": 0,
        "user_id": "$_id",
        "name": "$user.name",
        "department": "$user.department",
        "membership": "$user.membership",
        "books_borrowed": 1,
    }},
]

BORROWS_OVER_TIME = [
    {"$group": {
        "_id": {"$dateToString": {"format": "%Y-%m", "date": "$borrowed_at"}},
        "borrows": {"$sum": 1},
        "returns": {"$sum": {"$cond": [{"$eq": ["$status", "returned"]}, 1, 0]}},
    }},
    {"$sort": {"_id": 1}},
    {"$project": {"_id": 0, "month": "$_id", "borrows": 1, "returns": 1}},
]

LOAN_STATUS = [
    {"$group": {"_id": "$status", "count": {"$sum": 1}}},
    {"$sort": {"count": -1}},
    {"$project": {"_id": 0, "status": "$_id", "count": 1}},
]


def highest_rated(country: str | None = None) -> list:
    """Average rating recomputed from the individual ratings.

    Worth contrasting with `books.avg_rating`, which stores the same number
    on the book document (the Computed Pattern). The stored field makes
    reads cheap; this pipeline derives the value from source, one rating
    document at a time, and can therefore be used to verify it.

    Note the stage ORDER. The country filter cannot run first, because
    publisher.country lives on the book and the book only arrives after
    $lookup. Grouping happens on borrow_records, the join follows, and only
    then can country be matched.
    """
    pipeline = [
        {"$match": {"rating": {"$ne": None}}},
        {
            "$group": {
                "_id": "$book.book_id",
                "title": {"$first": "$book.title"},
                "average_rating": {"$avg": "$rating"},
                "ratings_counted": {"$sum": 1},
            }
        },
        # Ignore books with too few ratings for an average to mean anything.
        {"$match": {"ratings_counted": {"$gte": 5}}},
        {"$lookup": {"from": BOOKS, "localField": "_id", "foreignField": "_id", "as": "book"}},
        {"$unwind": "$book"},
    ]

    if country:
        pipeline.append({"$match": {"book.publisher.country": country}})

    pipeline += [
        {"$sort": {"average_rating": -1, "ratings_counted": -1}},
        {"$limit": 10},
        {
            "$project": {
                "_id": 0,
                "book_id": "$_id",
                "title": 1,
                "average_rating": {"$round": ["$average_rating", 2]},
                "ratings_counted": 1,
                "genre": "$book.genre",
                "publisher_country": "$book.publisher.country",
                "stored_avg_rating": "$book.avg_rating",
            }
        },
    ]
    return pipeline


# --------------------------------------------------------------------------
# registry
# --------------------------------------------------------------------------

PIPELINES = {
    "by_genre": (BOOKS, BY_GENRE),
    "price_distribution": (BOOKS, PRICE_DISTRIBUTION),
    "top_rated": (BOOKS, TOP_RATED),
    "by_decade": (BOOKS, BY_DECADE),
    "top_publishers": (BOOKS, TOP_PUBLISHERS),
    "popular_tags": (BOOKS, POPULAR_TAGS),
    "totals": (BOOKS, TOTALS),
    "most_borrowed": (BORROW_RECORDS, MOST_BORROWED),
    "highest_rated": (BORROW_RECORDS, highest_rated),
    "top_users": (BORROW_RECORDS, TOP_USERS),
    "borrows_over_time": (BORROW_RECORDS, BORROWS_OVER_TIME),
    "loan_status": (BORROW_RECORDS, LOAN_STATUS),
}


def resolve(entry, country: str | None = None) -> list:
    """A registry entry is either a fixed pipeline or one that takes filters."""
    source, pipeline = entry
    return pipeline(country) if callable(pipeline) else pipeline

# Everything sourced from `books` is also exposed as ONE $facet pipeline, so
# the dashboard computes seven metrics in a single pass over the collection
# instead of seven separate queries.
BOOKS_FACET = [
    _PRICE_AS_NUMBER,
    {"$facet": {
        "totals": TOTALS,
        "by_genre": BY_GENRE,
        # price_num already exists by this point, so drop the leading
        # $addFields stage from the sub-pipeline.
        "price_distribution": PRICE_DISTRIBUTION[1:],
        "top_rated": TOP_RATED,
        "by_decade": BY_DECADE,
        "top_publishers": TOP_PUBLISHERS,
        "popular_tags": POPULAR_TAGS,
    }},
]


def _run(source: str, pipeline: list) -> list:
    return [jsonable(doc) for doc in collection(source).aggregate(pipeline)]


@bp.get("/dashboard")
def dashboard():
    """Every dashboard metric.

    One $facet pass over `books`, plus four pipelines over `borrow_records`
    (which cannot be folded into the same $facet, since $facet operates on a
    single collection).
    """
    faceted = _run(BOOKS, BOOKS_FACET)
    books_part = faceted[0] if faceted else {}
    totals = books_part.get("totals") or [{}]

    return jsonify(
        {
            "totals": totals[0],
            "by_genre": books_part.get("by_genre", []),
            "price_distribution": books_part.get("price_distribution", []),
            "top_rated": books_part.get("top_rated", []),
            "by_decade": books_part.get("by_decade", []),
            "top_publishers": books_part.get("top_publishers", []),
            "popular_tags": books_part.get("popular_tags", []),
            "most_borrowed": _run(BORROW_RECORDS, MOST_BORROWED),
            "top_users": _run(BORROW_RECORDS, TOP_USERS),
            "borrows_over_time": _run(BORROW_RECORDS, BORROWS_OVER_TIME),
            "loan_status": _run(BORROW_RECORDS, LOAN_STATUS),
        }
    )


@bp.get("/pipelines")
def list_pipelines():
    """Name every available pipeline and the stages it uses."""
    out = []
    for name, entry in sorted(PIPELINES.items()):
        pipeline = resolve(entry)
        stages = [stage for doc in pipeline for stage in doc]
        out.append({"name": name, "collection": entry[0], "stages": stages})
    return jsonify({"count": len(out), "pipelines": out})


@bp.get("/<name>")
def run_pipeline(name: str):
    """Run one named pipeline and return it alongside its own definition.

    Returning the pipeline with the result lets a demo show the query and
    its output on the same screen. `?country=` is accepted by pipelines that
    take a filter, so the extra $match stage appears in the response as the
    filter is applied.
    """
    entry = PIPELINES.get(name)
    if entry is None:
        raise ApiError(404, "no such pipeline", f"available: {sorted(PIPELINES)}")

    source = entry[0]
    pipeline = resolve(entry, request.args.get("country") or None)

    return jsonify(
        {
            "name": name,
            "collection": source,
            "pipeline": pipeline,
            "stages": [stage for doc in pipeline for stage in doc],
            "result": _run(source, pipeline),
        }
    )
