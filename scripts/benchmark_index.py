"""Measure what indexes actually buy (Module 2 session 13, Module 4 session 29).

For each case: run the query with its index, drop the index, run the same
query again, then put the index back. The difference is the evidence.

Three numbers are reported and they are not equally trustworthy:

  docs examined          Server-side, deterministic, and the number that
                         actually proves the point. 2000 vs 200 is the
                         difference between reading the whole collection
                         and jumping straight to the matches.

  executionTimeMillis    Server-side query time from explain(). Reliable,
                         but small on a 2000-document collection.

  wall clock             Includes the round trip to Atlas in Mumbai, which
                         on a free-tier cluster can be larger than the query
                         itself. Reported for completeness; do not build the
                         report's argument on it.

Run:
    python -m scripts.benchmark_index
    python -m scripts.benchmark_index --runs 10
"""

import argparse
import os
import statistics
import time
from datetime import datetime, timezone

from app.core.indexes import INDEXES
from app.db import BOOKS, BORROW_RECORDS, close_client, get_db

DOCS_DIR = "docs"
OUTPUT = os.path.join(DOCS_DIR, "benchmarks.md")


# Each case names the index it depends on, so the script drops exactly that
# one and leaves the collection's other indexes alone.
CASES = [
    {
        "name": "Filter by genre",
        "collection": BOOKS,
        "index": "genre_year",
        "filter": {"genre": "Programming"},
        "sort": [("publication_year", -1)],
        "note": "compound index in ESR order - equality on genre, then sort on year",
    },
    {
        "name": "Exact author lookup",
        "collection": BOOKS,
        "index": "authors_idx",
        "filter": {"authors": "Mark Lutz"},
        "sort": None,
        "note": "a multikey index - authors is an array, so MongoDB indexes each element",
    },
    {
        "name": "Most borrowed, top 10",
        "collection": BOOKS,
        "index": "popularity",
        "filter": {},
        "sort": [("borrow_count", -1)],
        "limit": 10,
        "note": "without the index the server must sort all documents in memory",
    },
    {
        "name": "Books from one publisher",
        "collection": BOOKS,
        "index": "publisher_name",
        "filter": {"publisher.name": "O'Reilly Media"},
        "sort": None,
        "note": "dot notation into an embedded document is indexable like any other field",
    },
    {
        # filter is patched in main() with a real user_id - see the note.
        "name": "One user's open loans",
        "collection": BORROW_RECORDS,
        "index": "user_status",
        "filter": {"status": "borrowed"},
        "sort": None,
        "note": (
            "compound index on (user_id, status). Filtering on `status` ALONE "
            "cannot use it, because user_id is the leftmost prefix - that query "
            "collection-scans even though an index mentioning `status` exists"
        ),
    },
    {
        "name": "Overdue loans",
        "collection": BORROW_RECORDS,
        "index": "due_open_only",
        "filter": {"status": "borrowed", "due_date": {"$lt": datetime.now(timezone.utc)}},
        "sort": None,
        "note": "PARTIAL index - only loans still open are indexed, so the index stays small",
    },
]

# Stages that wrap the real access stage; peel them off to find IXSCAN or
# COLLSCAN underneath.
WRAPPER_STAGES = {"SORT", "SORT_KEY_GENERATOR", "LIMIT", "SKIP", "FETCH",
                  "PROJECTION_SIMPLE", "PROJECTION_COVERED", "SUBPLAN"}


def index_models():
    """collection -> {index name: IndexModel}, so a dropped index comes back exactly."""
    return {
        coll_name: {model.document["name"]: model for model in models}
        for coll_name, models in INDEXES.items()
    }


def access_stage(winning_plan: dict) -> str:
    node = winning_plan
    while node:
        stage = node.get("stage")
        if stage and stage not in WRAPPER_STAGES:
            return stage
        node = node.get("inputStage")
    return winning_plan.get("stage", "?")


def run_case(db, case, runs):
    coll = db[case["collection"]]

    def build():
        cursor = coll.find(case["filter"])
        if case.get("sort"):
            cursor = cursor.sort(case["sort"])
        if case.get("limit"):
            cursor = cursor.limit(case["limit"])
        return cursor

    plan = build().explain()
    execution = plan.get("executionStats", {})
    winning = plan.get("queryPlanner", {}).get("winningPlan", {})

    timings = []
    for _ in range(runs):
        start = time.perf_counter()
        list(build())
        timings.append((time.perf_counter() - start) * 1000)

    return {
        "stage": access_stage(winning),
        "docs_examined": execution.get("totalDocsExamined"),
        "keys_examined": execution.get("totalKeysExamined"),
        "returned": execution.get("nReturned"),
        "server_ms": execution.get("executionTimeMillis"),
        "wall_ms": round(statistics.median(timings), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Index benchmark: with vs without.")
    parser.add_argument("--runs", type=int, default=5, help="timed repetitions per query")
    args = parser.parse_args()

    db = get_db()
    models = index_models()

    # The user_status index is (user_id, status). A filter on status alone
    # cannot use it, so pick a real user_id - otherwise this case just
    # re-demonstrates the prefix rule instead of measuring the index.
    sample = db[BORROW_RECORDS].find_one({"status": "borrowed"}, {"user_id": 1})
    if sample:
        for case in CASES:
            if case["index"] == "user_status":
                case["filter"] = {"user_id": sample["user_id"], "status": "borrowed"}

    totals = {
        BOOKS: db[BOOKS].count_documents({}),
        BORROW_RECORDS: db[BORROW_RECORDS].count_documents({}),
    }
    print(f"books: {totals[BOOKS]} documents   "
          f"borrow_records: {totals[BORROW_RECORDS]} documents")
    print(f"each query run {args.runs}x, median reported\n")

    results = []

    for case in CASES:
        coll = db[case["collection"]]
        index_name = case["index"]
        print(f"  {case['name']:26} ", end="", flush=True)

        model = models.get(case["collection"], {}).get(index_name)
        if model is None:
            print("SKIPPED - index not defined")
            continue

        coll.create_indexes([model])
        with_index = run_case(db, case, args.runs)

        try:
            coll.drop_index(index_name)
            without_index = run_case(db, case, args.runs)
        finally:
            # Always put it back, even if the measurement raised.
            coll.create_indexes([model])

        results.append({"case": case, "with": with_index, "without": without_index})

        ew = with_index["docs_examined"] or 0
        ewo = without_index["docs_examined"] or 0
        factor = f"   ({ewo / ew:.0f}x more examined)" if ew and ewo > ew else ""
        print(f"{with_index['stage']:>8} {ew:>6} docs  ->  "
              f"{without_index['stage']:>8} {ewo:>6} docs{factor}")

    # ---- report ----------------------------------------------------------
    os.makedirs(DOCS_DIR, exist_ok=True)

    lines = [
        "# Index benchmarks",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} "
        "against MongoDB Atlas M0 (AWS Mumbai).",
        "",
        f"- `books`: **{totals[BOOKS]}** documents",
        f"- `borrow_records`: **{totals[BORROW_RECORDS]}** documents",
        f"- each query executed {args.runs}x, median wall time reported",
        "",
        "Method: run the query with its index, drop that index, run the same",
        "query again, recreate the index.",
        "",
        "**Read the `docs examined` column, not the timings.** It is",
        "deterministic and measured server-side. Wall time against a free-tier",
        "cloud cluster is dominated by the network round trip to Mumbai.",
        "",
        "| Query | Index | Stage (with) | Docs examined | Stage (without) | Docs examined | Reduction |",
        "|---|---|---|---|---|---|---|",
    ]

    for row in results:
        case, w, wo = row["case"], row["with"], row["without"]
        ew, ewo = w["docs_examined"] or 0, wo["docs_examined"] or 0
        reduction = f"**{ewo / ew:.0f}x**" if ew and ewo > ew else "-"
        lines.append(
            f"| {case['name']} | `{case['index']}` | {w['stage']} | {ew} | "
            f"{wo['stage']} | {ewo} | {reduction} |"
        )

    lines += [
        "",
        "## Query time",
        "",
        "| Query | Server ms (with) | Server ms (without) | Wall ms (with) | Wall ms (without) |",
        "|---|---|---|---|---|",
    ]
    for row in results:
        case, w, wo = row["case"], row["with"], row["without"]
        lines.append(
            f"| {case['name']} | {w['server_ms']} | {wo['server_ms']} | "
            f"{w['wall_ms']} | {wo['wall_ms']} |"
        )

    lines += ["", "## What each index is for", ""]
    for row in results:
        lines.append(f"- **`{row['case']['index']}`** — {row['case']['note']}")

    lines += [
        "",
        "## The compound-index prefix rule",
        "",
        "`genre_year` is `(genre, publication_year)`. A query filtering on",
        "`genre` uses it. A query filtering on `publication_year` **alone**",
        "cannot, because MongoDB only uses a compound index from its leftmost",
        "prefix. That query falls back to a full collection scan:",
        "",
        "```",
        "GET /books/explain?genre=Programming   ->  IXSCAN,    200 docs examined",
        "GET /books/explain?year_from=2000      ->  COLLSCAN, 2000 docs examined",
        "```",
        "",
        "This is why index field order matters, and why the compound index",
        "follows ESR: Equality first, then Sort, then Range.",
        "",
    ]

    with open(OUTPUT, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))

    print(f"\nwritten: {OUTPUT}")
    close_client()


if __name__ == "__main__":
    main()
