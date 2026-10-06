# BookVault — NoSQL Mini Project (AIM3141)

**Course:** AIM3141 NoSQL Database, MUJ AIML, 5th Sem, July–Dec 2026
**Marks:** 10 (Research Project / Mini Project slot)

**Stack:** Flask + PyMongo + MongoDB Atlas M0 + vanilla HTML/CSS/JS frontend
Framework decision: ported FastAPI → Flask on 2026-10-03. All PyMongo, aggregation,
`$jsonSchema`, index and transaction code is framework-independent and carried over
unchanged. Pydantic replaced by explicit validators in `app/core/validation.py`.

---

## Scope rule

**Build it only if it can be called from PyMongo.** Everything else is a report
section or an exam topic, not project work.

Justification from the handout's Lecture Plan — only these six sessions list
**Project** in the "Mode of Assessing CO" column:

| Session | Topic | Assessed by |
|---|---|---|
| 21 | Application connectivity using PyMongo | Project, MTE, ETE |
| 22 | Application connectivity using Node.js | Project, Quiz, ETE |
| 23 | Change Streams | Project, Quiz, ETE |
| 24 | CRUD app planning + schema design | Project review |
| 25 | CRUD app implementation + testing | Project evaluation |
| 36 | Comparative matrix + presentations | Project, ETE |

All of Module 4 (sharding 26, replica sets 27, security 28, backup/monitoring 29,
DBaaS 30) and the Module 1 theory (ACID/BASE 4, CAP 5, PACELC 6, partitioning 7)
are assessed by **Quiz / Assignment / MTE / ETE — never Project.**

**Dropped on 2026-10-04:** sharded Docker cluster, consistent-hashing simulation,
CAP stale-read demo, PACELC write-concern timing, Redis, Neo4j, vector search, RAG.

---

## Phase 1 — Foundation

- [x] Atlas M0 cluster `bookvault-cluster`, AWS Mumbai, MongoDB 8.0.34
- [x] DB user + IP whitelist
- [x] `.env` written
- [x] `.venv` created, `pip install -r requirements.txt` done (Flask)
- [x] Flask app imports cleanly; 21 routes registered
- [x] Validation layer + error handlers tested (7 reject cases)
- [x] `check_connection` → `ReplicaSetWithPrimary`, 3 nodes  **screenshot for Module 4**
- [x] 4 collections created with `$jsonSchema` validators
- [x] 13 indexes created
- [x] Flask app serves `/` route listing and `/health`

## Phase 2 — Data model + seed

- [x] 4 collections defined with validators: `books`, `users`, `borrow_records`, `reviews_overflow`
- [x] Correct BSON types in the validators: `Decimal128` price, `ISODate` dates, `ObjectId` refs
- [x] `scripts/seed_books.py` — **2000 books** from the Open Library API
      (real title/authors/year/publisher/subjects; synthesized price/copies)
- [x] Seeded **200 users**, **1500 borrow records**, 273 open / 129 overdue
- [x] Backfilled **Computed Pattern** fields (`avg_rating`, `borrow_count`, `review_count`)
- [x] **Outlier Pattern** live — 5 books at 63–75 reviews, `has_many_reviews: true`
- [x] Volume confirmed: COLLSCAN examines 2000 docs vs IXSCAN 200

## Phase 3 — CRUD + querying

- [x] `POST /books` create
- [x] `GET /books` list — filter, sort, paginate
- [x] `GET /books/<id>` read
- [x] `PATCH /books/<id>` partial update
- [x] `DELETE /books/<id>` delete
- [x] Projections — `?fields=title,authors`
- [x] Embedded document query — `?publisher_country=USA`
- [x] Array query — `?tag=programming`
- [x] Text search — `?q=clean+code`
- [x] Users CRUD (`app/routes/users.py`)
- [x] Borrow-history endpoint `/users/<id>/history` — Extended Reference, no `$lookup`
- [x] Validation rejection demo — `scripts/validation_demo.py`, **17/17 rejected**
      by MongoDB error code 121 via direct PyMongo inserts that bypass Flask

## Phase 4 — Indexing

- [x] 13 indexes defined: single-field, compound (ESR order), text with weights, partial, unique
- [x] `GET /books/explain` — returns IXSCAN/COLLSCAN, docs examined, keys examined, millis
- [x] `scripts/benchmark_index.py` — drop index, time query, recreate, time again
- [x] `docs/benchmarks.md` generated — 6 cases, all IXSCAN vs COLLSCAN
- [x] Prefix rule documented twice: `publication_year` alone and `status` alone
      both collection-scan despite an index naming that field

## Phase 5 — Aggregation

- [x] `GET /analytics/dashboard` — one `$facet` pipeline returning every books KPI
- [x] `GET /analytics/pipelines` and `GET /analytics/<name>` — run any pipeline alone
- [x] Books by genre — `$group` + `$sort`
- [x] Most borrowed — `$lookup` + `$unwind` + `$group`
- [x] Borrows over time — `$dateToString` (13 monthly buckets)
- [x] Price distribution — `$bucket` (5 bands)
- [x] Top users — `$lookup` into `users`
- [x] Popular tags — `$unwind` over the tags array
- [x] 11 pipelines total, stages: `$match $project $group $sort $limit $lookup $unwind $facet $bucket $addFields`
- [ ] `docs/aggregations.md` — each pipeline with its output

## Phase 6 — Transactions

- [x] `POST /borrow` — one transaction: insert borrow_record + decrement `copies.available`
- [x] `POST /borrow/return` — one transaction: close record + increment available + recompute `avg_rating`
- [x] `POST /borrow/demo-failure` — verified `rolled_back: true`, copies 1 → 1, records 1 → 1

## Phase 7 — Change Streams

- [x] Change-stream watcher on `books` + `borrow_records` via `db.watch()`
- [x] `GET /stream/events` — SSE endpoint (Flask generator + `text/event-stream`)
- [x] `GET /stream/demo` — standalone live page, no polling, no refresh
- [x] `GET /stream/info` — shows the watch pipeline, for the demo
- [x] ~~Node.js listener~~ — **dropped 2026-10-06 by decision.** Session 22
      ("Application connectivity using Node.js") is Project-assessed, but the
      project is Python-only; PyMongo covers session 21. Accepted trade-off.

## Phase 8 — Security

- [x] Password hashing — bcrypt direct, 72-byte guard (`app/core/security.py`)
- [x] JWT login — `POST /auth/login`, `GET /auth/me`, `GET /auth/permissions`
- [x] 3 roles: student / librarian / admin — `@require_auth` / `@require_role`
- [x] Role matrix verified: 401 anon / 403 wrong role / 200-201 correct role
- [x] Login returns an identical 401 for unknown email and wrong password
      (no account enumeration)
- [x] `.env` for secrets, git-ignored

## Phase 9 — Free evidence (no extra code)

- [ ] `scripts/check_connection.py` output — already prints `replSetGetStatus`, so
      Atlas M0 being a 3-node replica set is one screenshot
- [ ] Atlas monitoring tab — one screenshot
- [ ] `mongodump` then `mongorestore` — two commands

## Phase 10 — Frontend + deliverables

- [x] `scripts/demo_tour.py` — 11 sections, `--pause` / `--only` / `--list`,
      runs without the server (Flask test client). Verified end to end.
- [x] `docs/concept_map.md` — every Lecture Plan session mapped to
      CODE / EVIDENCE / REPORT / DROPPED with file + symbol references
- [x] Frontend — single page, 4 tabs, served at `/` from `app/static/index.html`
      Dashboard (Chart.js) · Books · Borrow & Return · Live Activity
      Each tab surfaces the MongoDB behind it: the pipeline JSON, the
      `explain()` plan, the raw document, the change-stream event.
- [x] `GET /api/demo-accounts` — populates the sign-in box (no password hashes)
- [ ] ~~`docs/report.md`~~ — **dropped 2026-10-07 by decision.** Theory topics
      covered verbally in the presentation instead.
- [ ] ~~`docs/comparative_matrix.md`~~ — dropped, same decision.
- [ ] `docs/report.md` — includes the short theory section (RDBMS limits, NoSQL
      types, ACID vs BASE, CAP, PACELC) and the session-36 comparative matrix
- [ ] Presentation deck
- [ ] README with setup steps

---

## Review

_(fill in as phases complete)_
