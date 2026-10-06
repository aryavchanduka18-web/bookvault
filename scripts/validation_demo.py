"""Prove the DATABASE rejects bad documents, not just the application.

Every insert here goes straight through PyMongo. The Flask layer, and
therefore app/core/validation.py, is never touched. Anything rejected below
was stopped by MongoDB's own $jsonSchema validator.

That is the point the report makes about NoSQL: "schema-less" does not mean
"structureless". A document database can enforce required fields, BSON
types, enums, numeric ranges, array constraints and string patterns - and it
enforces them for every client that connects (mongosh, Compass, a stray
script), not only for the one application that happens to validate first.

Run:
    python -m scripts.validation_demo
"""

from datetime import datetime, timezone

from bson import Decimal128
from pymongo.errors import OperationFailure

from app.db import BOOKS, USERS, close_client, get_db

DOCUMENT_VALIDATION_FAILURE = 121


def valid_book() -> dict:
    """A document satisfying every rule - the baseline each case mutates."""
    return {
        "title": "Validation Probe",
        "authors": ["Test Author"],
        "genre": "Programming",
        "publication_year": 2024,
        "price": Decimal128("499.00"),
        "publisher": {"name": "Test Press", "country": "India"},
        "copies": {"total": 3, "available": 3},
        "tags": ["testing"],
        "avg_rating": None,
        "borrow_count": 0,
        "review_count": 0,
        "has_many_reviews": False,
        "added_at": datetime.now(timezone.utc),
    }


def without(field: str) -> dict:
    doc = valid_book()
    doc.pop(field)
    return doc


def with_value(field: str, value) -> dict:
    doc = valid_book()
    doc[field] = value
    return doc


def valid_user() -> dict:
    return {
        "name": "Probe User",
        "email": "probe.user@muj.manipal.edu",
        "department": "AIML",
        "membership": "student",
        "role": "student",
        "joined_at": datetime.now(timezone.utc),
    }


def user_with(field: str, value) -> dict:
    doc = valid_user()
    doc[field] = value
    return doc


CASES = [
    (BOOKS, "missing required field `title`", without("title")),
    (BOOKS, "`title` is a number, not a string", with_value("title", 12345)),
    (BOOKS, "`title` is an empty string (minLength 1)", with_value("title", "")),
    (BOOKS, "`authors` is an empty array (minItems 1)", with_value("authors", [])),
    (BOOKS, "`authors` contains a number", with_value("authors", ["Real Name", 42])),
    (BOOKS, "`genre` is outside the enum", with_value("genre", "Cooking")),
    (BOOKS, "`publication_year` is 3024 (max 2100)", with_value("publication_year", 3024)),
    (BOOKS, "`publication_year` is a string", with_value("publication_year", "2024")),
    (BOOKS, "`price` is a float, not Decimal128", with_value("price", 499.00)),
    (BOOKS, "`copies.available` is negative", with_value("copies", {"total": 3, "available": -1})),
    (BOOKS, "`copies` is missing `available`", with_value("copies", {"total": 3})),
    (BOOKS, "`publisher` has no `name`", with_value("publisher", {"country": "India"})),
    (BOOKS, "`avg_rating` is 9.0 (max 5)", with_value("avg_rating", 9.0)),
    (BOOKS, "`added_at` is a string, not a date", with_value("added_at", "2026-10-06")),
    (USERS, "`email` fails the regex pattern", user_with("email", "not-an-email")),
    (USERS, "`membership` is outside the enum", user_with("membership", "alumni")),
    (USERS, "`role` is outside the enum", user_with("role", "superuser")),
]


def failing_properties(exc: OperationFailure) -> str:
    """Pull the readable part out of MongoDB's errInfo."""
    details = ((exc.details or {}).get("errInfo") or {}).get("details") or {}
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if "propertyName" in node:
                reasons = node.get("details") or node.get("reason") or ""
                text = ""
                if isinstance(reasons, list) and reasons:
                    first = reasons[0]
                    text = first.get("reason") or first.get("operatorName") or ""
                elif isinstance(reasons, str):
                    text = reasons
                found.append(f"{node['propertyName']}: {text}".strip(": "))
            if node.get("operatorName") == "required" and "missingProperties" in node:
                found.append("missing " + ", ".join(node["missingProperties"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(details)

    seen, unique = set(), []
    for item in found:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)

    return "; ".join(unique[:3]) or "schema rules not satisfied"


def main() -> None:
    db = get_db()

    print("Writing invalid documents DIRECTLY through PyMongo.")
    print("The Flask layer and app/core/validation.py are bypassed entirely,")
    print("so every rejection below comes from MongoDB's $jsonSchema validator.\n")
    print("=" * 80)

    rejected = accepted = 0

    for collection_name, label, document in CASES:
        try:
            result = db[collection_name].insert_one(document)
            accepted += 1
            print(f"  ACCEPTED  {collection_name:16} {label}")
            print("            ^ this should NOT have been accepted")
            db[collection_name].delete_one({"_id": result.inserted_id})
        except OperationFailure as exc:
            rejected += 1
            marker = "code 121" if exc.code == DOCUMENT_VALIDATION_FAILURE else f"code {exc.code}"
            print(f"  REJECTED  {collection_name:16} {label}")
            print(f"            {marker} - {failing_properties(exc)}")

    print("=" * 80)
    print(f"  rejected by the database: {rejected}/{len(CASES)}")
    if accepted:
        print(f"  WARNING: {accepted} invalid document(s) got through")

    # ---- control: a valid document must still be accepted ----------------
    print("\nControl - the same shape, valid this time:")
    probe = valid_book()
    probe["title"] = "Validation Probe (control)"
    try:
        result = db[BOOKS].insert_one(probe)
        print(f"  ACCEPTED  _id={result.inserted_id}")
        db[BOOKS].delete_one({"_id": result.inserted_id})
        print("  cleaned up")
    except OperationFailure as exc:
        print(f"  REJECTED unexpectedly: {exc}")

    print("\nTakeaway for the report:")
    print("  app/core/validation.py guards the HTTP layer.")
    print("  $jsonSchema guards the data itself, for every client that connects.")
    print("  Two independent layers, deliberately.")

    close_client()


if __name__ == "__main__":
    main()
