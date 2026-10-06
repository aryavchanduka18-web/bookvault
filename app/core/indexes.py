"""Index definitions (Module 2, session 13).

Every index here exists to serve a query the application actually issues.
scripts/benchmark_index.py measures each one against the same query with
the index dropped, which is the evidence the report is built on.
"""

from pymongo import ASCENDING, DESCENDING, TEXT, IndexModel

from app.db import BOOKS, BORROW_RECORDS, REVIEWS_OVERFLOW, USERS

BOOK_INDEXES = [
    # Full-text search over title/authors/tags. Title is weighted highest so
    # an exact title match outranks a book that merely mentions the term.
    IndexModel(
        [("title", TEXT), ("authors", TEXT), ("tags", TEXT)],
        name="book_text_search",
        weights={"title": 10, "authors": 5, "tags": 1},
    ),
    # Serves the commonest browse query: filter by genre, newest first.
    # Compound order follows ESR (Equality, Sort, Range).
    IndexModel(
        [("genre", ASCENDING), ("publication_year", DESCENDING)],
        name="genre_year",
    ),
    IndexModel([("authors", ASCENDING)], name="authors_idx"),
    # Powers the "most borrowed" leaderboard without a collection scan.
    IndexModel([("borrow_count", DESCENDING)], name="popularity"),
    IndexModel([("publisher.name", ASCENDING)], name="publisher_name"),
    # Partial index: only books with a copy on the shelf are indexed, so the
    # index stays small even as the collection grows.
    IndexModel(
        [("copies.available", ASCENDING)],
        name="available_partial",
        partialFilterExpression={"copies.available": {"$gt": 0}},
    ),
]

USER_INDEXES = [
    IndexModel([("email", ASCENDING)], name="email_unique", unique=True),
    IndexModel([("department", ASCENDING), ("membership", ASCENDING)], name="dept_membership"),
]

BORROW_INDEXES = [
    IndexModel([("user_id", ASCENDING), ("status", ASCENDING)], name="user_status"),
    IndexModel([("book.book_id", ASCENDING)], name="book_ref"),
    IndexModel([("borrowed_at", DESCENDING)], name="recent_first"),
    # Only open loans can be overdue, so the overdue sweep indexes just those.
    IndexModel(
        [("due_date", ASCENDING)],
        name="due_open_only",
        partialFilterExpression={"status": "borrowed"},
    ),
]

REVIEWS_OVERFLOW_INDEXES = [
    IndexModel([("book_id", ASCENDING)], name="book_id_idx"),
]

INDEXES = {
    BOOKS: BOOK_INDEXES,
    USERS: USER_INDEXES,
    BORROW_RECORDS: BORROW_INDEXES,
    REVIEWS_OVERFLOW: REVIEWS_OVERFLOW_INDEXES,
}
