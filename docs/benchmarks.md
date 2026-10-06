# Index benchmarks

Generated 2026-10-06 17:35 UTC against MongoDB Atlas M0 (AWS Mumbai).

- `books`: **2000** documents
- `borrow_records`: **1502** documents
- each query executed 5x, median wall time reported

Method: run the query with its index, drop that index, run the same
query again, recreate the index.

**Read the `docs examined` column, not the timings.** It is
deterministic and measured server-side. Wall time against a free-tier
cloud cluster is dominated by the network round trip to Mumbai.

| Query | Index | Stage (with) | Docs examined | Stage (without) | Docs examined | Reduction |
|---|---|---|---|---|---|---|
| Filter by genre | `genre_year` | IXSCAN | 200 | COLLSCAN | 2000 | **10x** |
| Exact author lookup | `authors_idx` | IXSCAN | 2 | COLLSCAN | 2000 | **1000x** |
| Most borrowed, top 10 | `popularity` | IXSCAN | 10 | COLLSCAN | 2000 | **200x** |
| Books from one publisher | `publisher_name` | IXSCAN | 2 | COLLSCAN | 2000 | **1000x** |
| One user's open loans | `user_status` | IXSCAN | 2 | COLLSCAN | 1502 | **751x** |
| Overdue loans | `due_open_only` | IXSCAN | 160 | COLLSCAN | 1502 | **9x** |

## Query time

| Query | Server ms (with) | Server ms (without) | Wall ms (with) | Wall ms (without) |
|---|---|---|---|---|
| Filter by genre | 0 | 1 | 52.1 | 52.7 |
| Exact author lookup | 0 | 1 | 20.5 | 20.4 |
| Most borrowed, top 10 | 0 | 1 | 21.4 | 23.1 |
| Books from one publisher | 0 | 1 | 21.8 | 20.6 |
| One user's open loans | 1 | 1 | 21.1 | 21.3 |
| Overdue loans | 1 | 1 | 43.0 | 43.2 |

## What each index is for

- **`genre_year`** — compound index in ESR order - equality on genre, then sort on year
- **`authors_idx`** — a multikey index - authors is an array, so MongoDB indexes each element
- **`popularity`** — without the index the server must sort all documents in memory
- **`publisher_name`** — dot notation into an embedded document is indexable like any other field
- **`user_status`** — compound index on (user_id, status). Filtering on `status` ALONE cannot use it, because user_id is the leftmost prefix - that query collection-scans even though an index mentioning `status` exists
- **`due_open_only`** — PARTIAL index - only loans still open are indexed, so the index stays small

## The compound-index prefix rule

`genre_year` is `(genre, publication_year)`. A query filtering on
`genre` uses it. A query filtering on `publication_year` **alone**
cannot, because MongoDB only uses a compound index from its leftmost
prefix. That query falls back to a full collection scan:

```
GET /books/explain?genre=Programming   ->  IXSCAN,    200 docs examined
GET /books/explain?year_from=2000      ->  COLLSCAN, 2000 docs examined
```

This is why index field order matters, and why the compound index
follows ESR: Equality first, then Sort, then Range.
