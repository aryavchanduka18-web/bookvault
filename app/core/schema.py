"""MongoDB $jsonSchema validators (Module 3, session 20).

These run inside the database engine, independent of the application.
Pydantic validates what enters the API; these validators guarantee nothing
invalid reaches a collection even when a write bypasses FastAPI entirely
(mongosh, Compass, a stray script). Both layers exist on purpose, and the
report explains the difference.
"""

from app.db import BOOKS, BORROW_RECORDS, REVIEWS_OVERFLOW, USERS

GENRES = [
    "Programming",
    "Fiction",
    "Science",
    "History",
    "Mathematics",
    "Philosophy",
    "Biography",
    "Technology",
    "Poetry",
    "Reference",
]

MEMBERSHIPS = ["student", "faculty", "staff"]
ROLES = ["student", "librarian", "admin"]
BORROW_STATUSES = ["borrowed", "returned", "overdue"]

# Books above this review count are treated as outliers: their reviews are
# spilled into reviews_overflow instead of growing the book document without
# bound. This is the Outlier Pattern (Module 2, session 14).
OUTLIER_REVIEW_THRESHOLD = 50


BOOK_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "title": "Book document validation",
        "required": [
            "title",
            "authors",
            "genre",
            "publication_year",
            "price",
            "publisher",
            "copies",
            "added_at",
        ],
        "properties": {
            "title": {
                "bsonType": "string",
                "minLength": 1,
                "maxLength": 300,
                "description": "required, non-empty string",
            },
            "authors": {
                "bsonType": "array",
                "minItems": 1,
                "items": {"bsonType": "string"},
                "description": "required, at least one author",
            },
            "genre": {
                "enum": GENRES,
                "description": f"must be one of {GENRES}",
            },
            "publication_year": {
                "bsonType": "int",
                "minimum": 1450,
                "maximum": 2100,
                "description": "int32, Gutenberg press onwards",
            },
            "price": {
                "bsonType": "decimal",
                "description": "Decimal128 - money is never stored as a float",
            },
            "publisher": {
                "bsonType": "object",
                "required": ["name"],
                "properties": {
                    "name": {"bsonType": "string", "minLength": 1},
                    "country": {"bsonType": "string"},
                },
                "description": "embedded document - always read with the book",
            },
            "copies": {
                "bsonType": "object",
                "required": ["total", "available"],
                "properties": {
                    "total": {"bsonType": "int", "minimum": 0},
                    "available": {"bsonType": "int", "minimum": 0},
                },
            },
            "tags": {
                "bsonType": "array",
                "items": {"bsonType": "string"},
            },
            # --- Computed Pattern (Module 2, session 15) ---
            # Maintained on write so read-heavy dashboard queries never
            # recompute these from borrow_records at request time.
            "avg_rating": {
                "bsonType": ["double", "null"],
                "minimum": 0,
                "maximum": 5,
            },
            "borrow_count": {"bsonType": "int", "minimum": 0},
            "review_count": {"bsonType": "int", "minimum": 0},
            # --- Outlier Pattern (Module 2, session 14) ---
            "has_many_reviews": {
                "bsonType": "bool",
                "description": "true when reviews live in reviews_overflow",
            },
            "added_at": {"bsonType": "date"},
        },
    }
}


USER_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "title": "User document validation",
        "required": ["name", "email", "department", "membership", "role", "joined_at"],
        "properties": {
            "name": {"bsonType": "string", "minLength": 1, "maxLength": 120},
            "email": {
                "bsonType": "string",
                "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                "description": "must be a valid email address",
            },
            "department": {"bsonType": "string"},
            "membership": {"enum": MEMBERSHIPS},
            "role": {"enum": ROLES},
            "password_hash": {"bsonType": "string"},
            "joined_at": {"bsonType": "date"},
        },
    }
}


BORROW_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "title": "Borrow record validation",
        "required": ["user_id", "book", "borrowed_at", "due_date", "status"],
        "properties": {
            "user_id": {"bsonType": "objectId"},
            # --- Extended Reference Pattern (Module 2, session 15) ---
            # The book_id is the real reference; title and authors are a
            # denormalised snapshot so listing a user's history needs no
            # $lookup. Snapshot fields are intentionally NOT kept in sync -
            # a borrow record should show the title as it was at borrow time.
            "book": {
                "bsonType": "object",
                "required": ["book_id", "title", "authors"],
                "properties": {
                    "book_id": {"bsonType": "objectId"},
                    "title": {"bsonType": "string"},
                    "authors": {"bsonType": "array", "items": {"bsonType": "string"}},
                },
            },
            "borrowed_at": {"bsonType": "date"},
            "due_date": {"bsonType": "date"},
            "returned_at": {"bsonType": ["date", "null"]},
            "status": {"enum": BORROW_STATUSES},
            "rating": {"bsonType": ["int", "null"], "minimum": 1, "maximum": 5},
        },
    }
}


REVIEWS_OVERFLOW_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "title": "Review overflow bucket (Outlier Pattern)",
        "required": ["book_id", "reviews"],
        "properties": {
            "book_id": {"bsonType": "objectId"},
            "reviews": {
                "bsonType": "array",
                "items": {
                    "bsonType": "object",
                    "required": ["user_id", "rating", "created_at"],
                    "properties": {
                        "user_id": {"bsonType": "objectId"},
                        "rating": {"bsonType": "int", "minimum": 1, "maximum": 5},
                        "text": {"bsonType": "string", "maxLength": 2000},
                        "created_at": {"bsonType": "date"},
                    },
                },
            },
        },
    }
}


VALIDATORS = {
    BOOKS: BOOK_VALIDATOR,
    USERS: USER_VALIDATOR,
    BORROW_RECORDS: BORROW_VALIDATOR,
    REVIEWS_OVERFLOW: REVIEWS_OVERFLOW_VALIDATOR,
}
