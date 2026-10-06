"""Create every index defined in app/core/indexes.py.

Idempotent: create_indexes is a no-op for indexes that already exist with
the same specification.

Run:  python -m scripts.setup_indexes
"""

from app.core.indexes import INDEXES
from app.db import close_client, get_db


def main() -> None:
    db = get_db()

    for name, models in INDEXES.items():
        created = db[name].create_indexes(models)
        print(f"{name:20} {len(created)} indexes ensured: {', '.join(created)}")

    print("\ncurrent indexes:")
    for name in INDEXES:
        for index_name, spec in db[name].index_information().items():
            print(f"  {name}.{index_name:24} {spec.get('key')}")

    close_client()


if __name__ == "__main__":
    main()
