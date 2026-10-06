# BookVault — syllabus concept map

**AIM3141 NoSQL Database** · Manipal University Jaipur · B.Tech AIML, 5th Semester
**Project:** BookVault — a MongoDB-backed digital library
**Stack:** Flask · PyMongo · MongoDB Atlas M0 (3-node replica set, AWS Mumbai) · MongoDB 8.0.34

Every row below maps a topic from the course Lecture Plan to where it lives in this
project and how to show it working.

References are by **file and symbol**, not line number, so they do not go stale.

**Status key**

| | meaning |
|---|---|
| **CODE** | implemented and running against the live cluster |
| **EVIDENCE** | measured, with output captured in `docs/` |
| **REPORT** | written up, not implemented — reason stated |
| **DROPPED** | deliberately out of scope — reason stated |

> **Note (2026-10-07):** `docs/report.md` and `docs/comparative_matrix.md` were
> dropped by decision. Rows below marked REPORT are covered **verbally in the
> presentation**, not in a written document. Everything marked CODE or EVIDENCE
> is in the repository and runs.

**One command demonstrates most of this:**

```
python -m scripts.demo_tour            # all 11 sections
python -m scripts.demo_tour --pause    # stop between sections
python -m scripts.demo_tour --only transactions
```

---

## Module 1 — Foundations of NoSQL and Distributed Systems

| # | Topic | Status | Where / how |
|---|---|---|---|
| 1 | RDBMS limitations, motivation for NoSQL | REPORT | `docs/report.md` §1. The flexible `books` schema is the practical argument: different books carry different tags, publishers and page counts with no migration. |
| 2 | Evolution and industry need | REPORT | `docs/report.md` §1 |
| 3 | NoSQL types: document, key-value, column-family, graph | REPORT | `docs/comparative_matrix.md`. BookVault is a **document** store. |
| 4 | **ACID vs BASE** | **CODE** | `app/routes/borrow.py` → `_do_borrow()`, `return_book()`. Borrow/return run as one transaction — strict ACID inside a document database. Demo: `demo_tour --only transactions` |
| 5 | CAP theorem, eventual consistency | REPORT | `docs/report.md` §2. Atlas M0 is CP: on a partition the minority side stops accepting writes rather than diverging. |
| 6 | PACELC | REPORT | `docs/report.md` §2. The Else-branch is visible in the default write concern `w:majority` — latency traded for consistency. |
| 7 | Partitioning, consistent hashing | REPORT | `docs/report.md` §2. Sharding not deployed — see Module 4. |

## Module 2 — MongoDB architecture and basic operations

| # | Topic | Status | Where / how |
|---|---|---|---|
| 8 | Shell, Compass, Atlas | **CODE** | Live Atlas cluster `bookvault-cluster`. `scripts/check_connection.py` prints the topology; Compass connects with the same URI. |
| 9 | Documents, collections, **BSON types** | **CODE** | `app/core/schema.py`. `price` → `Decimal128`, `added_at` → `ISODate`, `_id` → `ObjectId`, `authors`/`tags` → arrays, `publisher`/`copies` → embedded documents. Demo: `--only model` |
| 10 | CRUD — insert, find | **CODE** | `app/routes/books.py` → `create_book()`, `get_book()`, `list_books()` |
| 11 | CRUD — update, delete | **CODE** | `app/routes/books.py` → `update_book()` (PATCH, partial), `delete_book()` |
| 12 | Projections, querying embedded documents | **CODE** | `app/routes/books.py` → `_build_projection()`, `_build_filter()`. `?fields=title,authors`, `?publisher_country=UK` (dot notation), `?tag=fiction` (array match) |
| 13 | Filtering, sorting, **indexing** | **CODE + EVIDENCE** | `app/core/indexes.py` — 13 indexes: single-field, compound (ESR order), text with weights, partial, unique. `GET /books/explain`. Numbers in `docs/benchmarks.md` |
| 14 | **Outlier Pattern** | **CODE** | `app/core/schema.py` → `OUTLIER_REVIEW_THRESHOLD`, the `has_many_reviews` flag, and the `reviews_overflow` collection. 5 books currently flagged, at 63–75 reviews. |
| 15 | **Computed Pattern**, **Extended Reference Pattern** | **CODE** | Computed: `avg_rating` / `borrow_count` / `review_count` stored on the book, maintained by `app/routes/borrow.py` → `_recompute_book_ratings()`. Extended Reference: `borrow_records.book = {book_id, title, authors}` — see `app/routes/users.py` → `user_history()`, which renders a full history with **no `$lookup`**. |

## Module 3 — Aggregation and application development

| # | Topic | Status | Where / how |
|---|---|---|---|
| 16 | Aggregation pipeline and stages | **CODE** | `app/routes/analytics.py` — 11 named pipelines. `GET /analytics/pipelines` lists each with the stages it uses. |
| 17 | `$match` `$project` `$group` `$sort` | **CODE** | `BY_GENRE`, `TOP_RATED`, `TOTALS`, `LOAN_STATUS` |
| 18 | `$lookup` `$unwind` `$facet` | **CODE** | `MOST_BORROWED` (`$lookup` + `$unwind`), `POPULAR_TAGS` (`$unwind`), `BOOKS_FACET` (7 metrics in one pass). Also `$bucket` in `PRICE_DISTRIBUTION` and `$dateToString` in `BORROWS_OVER_TIME`. |
| 19 | Embedded vs referenced — modelling analysis | **CODE + REPORT** | Both used deliberately. `publisher` embedded (always read together, bounded). `borrow_records` referenced (unbounded, queried independently). Reasoning in `docs/report.md` §4. |
| 20 | **Schema validation** | **CODE + EVIDENCE** | `app/core/schema.py` → `VALIDATORS`, applied by `scripts/setup_collections.py` with `validationAction: "error"`. `scripts/validation_demo.py` — **17/17 invalid documents rejected** with error code **121**, inserted directly through PyMongo so Flask is bypassed entirely. |
| 21 | **Application connectivity — PyMongo** | **CODE** | The entire backend. `app/db.py` → one shared `MongoClient`; every route and script uses it. |
| 22 | Application connectivity — Node.js | **DROPPED** | Project is Python-only by decision (2026-10-06). PyMongo covers application connectivity; a second driver would add a second language to explain for one checkbox. |
| 23 | **Change Streams, reactive updates** | **CODE** | `app/routes/stream.py` → `_event_stream()` using `db.watch()` with `full_document="updateLookup"`, delivered over Server-Sent Events. `GET /stream/demo` is a live page with no polling. Demo: `--only changestreams` |
| 24 | CRUD app — planning and schema design | **CODE** | `tasks/todo.md` (plan + scope rule), `app/core/schema.py` (4 collections), this document. |
| 25 | CRUD app — implementation and testing | **CODE** | 27 routes across 6 blueprints. `scripts/demo_tour.py` exercises them. |

## Module 4 — Advanced concepts and cloud ecosystem

| # | Topic | Status | Where / how |
|---|---|---|---|
| 26 | Sharding, shard-key selection | REPORT | Not deployed — Atlas M0 cannot shard (needs M30+), and a local Docker cluster was out of scope. `docs/report.md` §5 analyses the shard key: `genre` is low-cardinality and would create jumbo chunks; a hashed `_id` distributes evenly but destroys targeted range queries. |
| 27 | **Replica sets, failover, high availability** | **CODE + EVIDENCE** | Atlas M0 **is** a 3-node replica set. `scripts/check_connection.py` prints `replSetGetStatus` — 1 PRIMARY, 2 SECONDARY. This is also *why* transactions and change streams work here; neither runs on a standalone `mongod`. |
| 28 | **Security: users, roles, authentication** | **CODE** | Two independent layers. **Application:** `app/core/auth.py` → `require_auth`, `require_role`; JWT; bcrypt in `app/core/security.py`; 3 roles. **Database:** the Atlas user's `readWrite` role, set in Atlas → Database Access. Matrix at `GET /auth/permissions`. Demo: `--only security` |
| 28b | Encryption at rest / in transit | REPORT | Atlas encrypts at rest by default; `mongodb+srv://` connections are TLS. Inherited from Atlas, not configured by this project. |
| 29 | Backup, restore, monitoring, **performance** | **EVIDENCE** | Performance: `scripts/benchmark_index.py` → `docs/benchmarks.md`, 6 queries measured with and without their index. Monitoring: Atlas → View Monitoring. Backup: `mongodump` / `mongorestore`. |
| 30 | DBaaS — Atlas vs AWS DynamoDB | REPORT | `docs/comparative_matrix.md` |

## Module 5 — Vector databases and polyglot case studies

| # | Topic | Status | Where / how |
|---|---|---|---|
| 31–34 | Vector databases, cosine/Euclidean similarity, ANN, Pinecone/Milvus/Weaviate | REPORT | `docs/comparative_matrix.md`. Not implemented — BookVault has no semantic-search requirement, and adding one would not demonstrate a MongoDB concept. |
| 35 | Redis caching, Neo4j recommendations | REPORT | `docs/comparative_matrix.md` — where each would fit in a library system, and why neither is justified at this scale. |
| 36 | Simple RAG back-end; comparative matrix; presentation | REPORT | `docs/comparative_matrix.md` + the presentation deck. RAG not implemented — scope decision recorded in `tasks/todo.md`. |

---

## Scope rule used throughout

> **Build it only if it can be called from PyMongo.**

That line decided every inclusion above. Replica sets, transactions, change streams,
`$jsonSchema` and `explain()` are all PyMongo calls, so they are code. CAP, PACELC,
consistent hashing and the vector-database family are not, so they are written up.

Justification from the handout's own Lecture Plan: only six sessions list **Project**
under *Mode of Assessing CO* — 21 (PyMongo), 22 (Node.js), 23 (Change Streams),
24 and 25 (the CRUD application), and 36 (comparative matrix and presentation).
Every Module 4 infrastructure topic and every Module 1 theory topic is assessed by
Quiz, Assignment, MTE or ETE — not by the project.

---

## Where everything lives

```
app/
  config.py              .env loading
  db.py                  shared MongoClient, collection name constants
  core/
    schema.py            $jsonSchema validators, GENRES, OUTLIER_REVIEW_THRESHOLD
    indexes.py           13 index definitions
    validation.py        request validation (application layer)
    serializers.py       BSON -> JSON (ObjectId, Decimal128, datetime)
    security.py          bcrypt password hashing
    auth.py              JWT, require_auth, require_role
    errors.py            ApiError + Flask handlers, catches MongoDB code 121
  routes/
    auth.py              login, /auth/me, /auth/permissions
    books.py             CRUD, filtering, projection, sorting, text search, explain
    users.py             user CRUD, borrow history (Extended Reference payoff)
    borrow.py            transactions, rollback demo, Computed Pattern maintenance
    analytics.py         11 aggregation pipelines, $facet dashboard
    stream.py            change streams over SSE, live demo page
  main.py                app factory, blueprint registration

scripts/
  check_connection.py    topology + replica set status
  setup_collections.py   create collections with validators
  setup_indexes.py       create all 13 indexes
  seed_books.py          2000 books from Open Library + users + borrow records
  benchmark_index.py     with/without index measurement -> docs/benchmarks.md
  validation_demo.py     17 invalid inserts, all rejected by the database
  demo_tour.py           guided tour of every concept

docs/
  concept_map.md         this file
  benchmarks.md          generated index measurements
  report.md              written report
  comparative_matrix.md  Module 5 write-up
```

## Data at the time of writing

| Collection | Documents |
|---|---|
| `books` | 2000 |
| `users` | 200 |
| `borrow_records` | 1502 |
| `reviews_overflow` | 5 |

Book bibliographic data — title, authors, publication year, publisher, subjects — is
**real**, taken from the public Open Library API. Price, copy counts, users, loans and
ratings are **synthesized**; Open Library holds no such data. Stated plainly so
nothing in the demo is overclaimed.
