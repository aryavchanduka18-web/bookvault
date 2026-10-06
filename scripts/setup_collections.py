"""Create the four collections with their $jsonSchema validators.

Idempotent: creates a collection if missing, otherwise applies the validator
with collMod. Run after any change to app/core/schema.py.

Run:  python -m scripts.setup_collections
"""

from pymongo.errors import CollectionInvalid

from app.core.schema import VALIDATORS
from app.db import ALL_COLLECTIONS, close_client, get_db


def main() -> None:
    db = get_db()
    existing = set(db.list_collection_names())

    for name in ALL_COLLECTIONS:
        validator = VALIDATORS[name]
        if name in existing:
            db.command(
                "collMod",
                name,
                validator=validator,
                validationLevel="strict",
                validationAction="error",
            )
            print(f"{name:20} validator updated")
        else:
            try:
                db.create_collection(
                    name,
                    validator=validator,
                    validationLevel="strict",
                    validationAction="error",
                )
                print(f"{name:20} created with validator")
            except CollectionInvalid:
                print(f"{name:20} already exists, skipped")

    close_client()


if __name__ == "__main__":
    main()
