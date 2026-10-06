"""One command that walks every MongoDB concept in the project, in order.

Built for the viva. Instead of clicking around Postman and hoping the right
window is open, run this and narrate the output. Every section prints what
it demonstrates, the call it makes, and the real result from Atlas.

    python -m scripts.demo_tour                 # run everything
    python -m scripts.demo_tour --pause         # stop between sections
    python -m scripts.demo_tour --only crud     # run one section
    python -m scripts.demo_tour --list          # list section keys

Nothing here needs the Flask server running - it drives the app through
Flask's test client, so the whole tour works from one terminal.
"""

import argparse
import json
import threading
import time
from datetime import datetime, timezone

from pymongo.errors import OperationFailure

from app.core.schema import OUTLIER_REVIEW_THRESHOLD, VALIDATORS
from app.db import (
    ALL_COLLECTIONS,
    BOOKS,
    BORROW_RECORDS,
    REVIEWS_OVERFLOW,
    USERS,
    close_client,
    get_client,
    get_db,
)
from app.main import app

WIDTH = 78
SEED_PASSWORD = "bookvault123"


# --------------------------------------------------------------------------
# output helpers
# --------------------------------------------------------------------------

def banner(number: str, title: str, syllabus: str) -> None:
    print()
    print("=" * WIDTH)
    print(f"  {number}. {title}")
    print(f"      syllabus: {syllabus}")
    print("=" * WIDTH)


def step(text: str) -> None:
    print(f"\n  > {text}")


def show(label: str, value) -> None:
    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, indent=2, default=str)
        indented = "\n".join("      " + line for line in rendered.splitlines())
        print(f"    {label}:\n{indented}")
    else:
        print(f"    {label}: {value}")


def note(text: str) -> None:
    print(f"    ... {text}")


# --------------------------------------------------------------------------
# shared context
# --------------------------------------------------------------------------

class Tour:
    def __init__(self):
        self.client = app.test_client()
        self.db = get_db()
        self.tokens = {}
        self.book_id = None
        self.user_id = None

    def login_all(self):
        for role in ("student", "librarian", "admin"):
            user = self.db[USERS].find_one({"role": role})
            if not user:
                continue
            response = self.client.post(
                "/auth/login",
                json={"email": user["email"], "password": SEED_PASSWORD},
            )
            if response.status_code == 200:
                self.tokens[role] = response.get_json()["access_token"]

    def auth(self, role="librarian"):
        token = self.tokens.get(role)
        return {"Authorization": f"Bearer {token}"} if token else {}

    def pick_ids(self):
        book = self.db[BOOKS].find_one({"copies.available": {"$gt": 1}})
        user = self.db[USERS].find_one({"role": "student"})
        self.book_id = str(book["_id"]) if book else None
        self.user_id = str(user["_id"]) if user else None


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

def s_connection(t: Tour):
    """Cloud deployment, replica set, high availability"""
    banner("01", "Cloud deployment and replication",
           "Module 4 - Atlas, replica sets, high availability")

    client = get_client()
    step("the driver discovers the cluster topology by itself")
    show("topology", client.topology_description.topology_type_name)
    show("nodes", [f"{h}:{p}" for h, p in client.nodes])
    note("ReplicaSetWithPrimary + 3 nodes = Atlas M0 is a 3-node replica set")
    note("that is WHY transactions and change streams work here")

    try:
        status = client.admin.command("replSetGetStatus")
        step("replSetGetStatus")
        for member in status["members"]:
            print(f"      {member['stateStr']:10} {member['name']}")
    except OperationFailure:
        note("replSetGetStatus needs cluster-monitor rights; the topology above is enough")


def s_model(t: Tour):
    """Documents, collections, BSON types"""
    banner("02", "Document model and BSON types",
           "Module 2 session 9 - documents, collections, BSON")

    step("collections in this database")
    for name in ALL_COLLECTIONS:
        print(f"      {name:18} {t.db[name].count_documents({}):>6} documents")

    step("one raw document, straight from PyMongo (not JSON-converted)")
    doc = t.db[BOOKS].find_one({"avg_rating": {"$ne": None}})
    for field in ("_id", "title", "authors", "price", "publisher", "copies",
                  "tags", "avg_rating", "added_at"):
        value = doc.get(field)
        print(f"      {field:14} {type(value).__name__:12} {str(value)[:42]}")

    note("price is Decimal128, not float - money must not use binary floating point")
    note("added_at is a real BSON date; _id is an ObjectId; authors/tags are arrays")
    note("publisher is an EMBEDDED document, always read with its book")


def s_validation(t: Tour):
    """$jsonSchema validation enforced by the database"""
    banner("03", "Schema validation", "Module 3 session 20 - $jsonSchema")

    step("the validator attached to `books`")
    rules = VALIDATORS[BOOKS]["$jsonSchema"]
    show("required", rules["required"])
    show("genre rule", rules["properties"]["genre"])
    show("price rule", rules["properties"]["price"])

    step("inserting an invalid document DIRECTLY through PyMongo")
    note("the Flask layer and app/core/validation.py are bypassed completely")
    bad = {
        "title": 12345,
        "authors": [],
        "genre": "Cooking",
        "publication_year": 3024,
        "price": 499.00,
        "publisher": {"country": "India"},
        "copies": {"total": 3},
        "added_at": "2026-10-06",
    }
    try:
        t.db[BOOKS].insert_one(bad)
        print("    ACCEPTED - this should not happen")
    except OperationFailure as exc:
        show("error code", exc.code)
        note("code 121 = DocumentValidationFailure, raised by MongoDB itself")

    note("run `python -m scripts.validation_demo` for all 17 rejection cases")


def s_patterns(t: Tour):
    """Computed, Extended Reference and Outlier patterns"""
    banner("04", "Schema design patterns",
           "Module 2 sessions 14-15 - Computed, Extended Reference, Outlier")

    step("COMPUTED PATTERN - totals stored on the book, not recalculated on read")
    book = t.db[BOOKS].find_one({"review_count": {"$gt": 3}})
    show("book", {k: book.get(k) for k in
                  ("title", "avg_rating", "review_count", "borrow_count")})
    note("the dashboard reads these directly; no aggregation at request time")
    note("maintained inside the borrow/return transaction, see app/routes/borrow.py")

    step("EXTENDED REFERENCE - borrow_records carries a snapshot of the book")
    record = t.db[BORROW_RECORDS].find_one({})
    show("borrow_record.book", {k: str(v)[:44] for k, v in record["book"].items()})
    note("book_id is the real reference; title/authors are denormalised")
    note("so a user's history renders with ONE query and no $lookup")
    note("deliberately NOT kept in sync - a loan should show the title as it was")

    step("OUTLIER PATTERN - books with unusually many reviews")
    for o in t.db[BOOKS].find({"has_many_reviews": True},
                              {"title": 1, "review_count": 1}).limit(5):
        print(f"      {o['review_count']:>4} reviews   {o['title'][:48]}")
    show("threshold", OUTLIER_REVIEW_THRESHOLD)
    show("overflow buckets", t.db[REVIEWS_OVERFLOW].count_documents({}))
    note("review bodies move to reviews_overflow so book documents stay bounded")


def s_crud(t: Tour):
    """Create, read, update, delete"""
    banner("05", "CRUD", "Module 2 sessions 10-11")

    headers = t.auth("librarian")
    payload = {
        "title": "Demo Tour Book",
        "authors": ["Demo Author"],
        "genre": "Programming",
        "publication_year": 2026,
        "price": 777.50,
        "publisher": {"name": "Demo Press", "country": "India"},
        "copies": {"total": 4, "available": 4},
        "tags": ["demo", "nosql"],
    }

    step("CREATE - POST /books")
    created = t.client.post("/books", json=payload, headers=headers).get_json()
    book_id = created["id"]
    show("id", book_id)
    show("computed fields seeded at zero",
         {k: created[k] for k in
          ("avg_rating", "borrow_count", "review_count", "has_many_reviews")})

    step("READ - GET /books/<id>")
    show("title", t.client.get(f"/books/{book_id}").get_json()["title"])

    step("UPDATE - PATCH /books/<id>  (partial, not a full replace)")
    patched = t.client.patch(
        f"/books/{book_id}",
        json={"price": 899.00, "tags": ["demo", "updated"]},
        headers=headers,
    ).get_json()
    show("price", patched["price"])
    show("tags", patched["tags"])
    show("title untouched", patched["title"])

    step("DELETE - DELETE /books/<id>")
    show("status", t.client.delete(f"/books/{book_id}", headers=headers).status_code)
    show("now returns", t.client.get(f"/books/{book_id}").status_code)


def s_query(t: Tour):
    """Filtering, projection, sorting, embedded and array queries"""
    banner("06", "Querying, projection, sorting", "Module 2 sessions 12-13")

    step("filter by genre, sorted by year")
    r = t.client.get("/books?genre=Programming&sort=year:desc&limit=3").get_json()
    show("matching", r["total"])
    for b in r["items"]:
        print(f"      {b['publication_year']}  {b['title'][:52]}")

    step("PROJECTION - ?fields=title,authors returns only those fields")
    r = t.client.get("/books?fields=title,authors&limit=2").get_json()
    show("keys returned", sorted(r["items"][0].keys()))

    step("EMBEDDED DOCUMENT query - publisher.country, via dot notation")
    show("books from UK publishers",
         t.client.get("/books?publisher_country=UK&limit=2").get_json()["total"])

    step("ARRAY query - a match on `tags` matches any element")
    show("tagged 'fiction'",
         t.client.get("/books?tag=fiction&limit=2").get_json()["total"])

    step("TEXT SEARCH - $text against the weighted text index")
    r = t.client.get("/books?q=python&limit=3").get_json()
    show("matches", r["total"])
    for b in r["items"]:
        print(f"      {b['title'][:60]}")

    step("RANGE query")
    show("published 2010-2015",
         t.client.get("/books?year_from=2010&year_to=2015").get_json()["total"])


def s_indexing(t: Tour):
    """Indexes, explain(), IXSCAN vs COLLSCAN"""
    banner("07", "Indexing and query plans",
           "Module 2 session 13, Module 4 session 29")

    step("indexes defined on `books`")
    for name, spec in t.db[BOOKS].index_information().items():
        print(f"      {name:22} {spec.get('key')}")

    step("INDEXED query - GET /books/explain?genre=Programming")
    e = t.client.get("/books/explain?genre=Programming").get_json()
    show("docs examined", e["docs_examined"])
    show("docs returned", e["docs_returned"])

    step("UNINDEXED field - GET /books/explain?year_from=2000&year_to=2005")
    e2 = t.client.get("/books/explain?year_from=2000&year_to=2005").get_json()
    show("docs examined", e2["docs_examined"])
    show("docs returned", e2["docs_returned"])

    note("the compound index is (genre, publication_year)")
    note("a query on publication_year ALONE cannot use it - leftmost-prefix rule")
    note("so it scans every document, even though the index names that field")
    note("this is why compound index order follows ESR: Equality, Sort, Range")
    note("full numbers: docs/benchmarks.md  (python -m scripts.benchmark_index)")


def s_aggregation(t: Tour):
    """Aggregation pipelines, $lookup, $unwind, $facet, $bucket"""
    banner("08", "Aggregation framework",
           "Module 3 sessions 16-18 - pipeline, operators, $lookup, $facet")

    step("every pipeline in the project and the stages it uses")
    for p in t.client.get("/analytics/pipelines").get_json()["pipelines"]:
        print(f"      {p['name']:20} {p['collection']:16} {' '.join(p['stages'])}")

    for name, label in [
        ("by_genre", "$group + $sort"),
        ("price_distribution", "$bucket - price bands"),
        ("popular_tags", "$unwind - count tags across the collection"),
        ("most_borrowed", "$lookup + $unwind - join borrow_records to books"),
        ("top_users", "$lookup into users"),
        ("borrows_over_time", "$dateToString - monthly buckets"),
    ]:
        step(f"{name}   ({label})")
        result = t.client.get(f"/analytics/{name}").get_json()["result"]
        for row in result[:4]:
            print("      " + json.dumps(row, default=str))
        if len(result) > 4:
            print(f"      ... {len(result) - 4} more")

    step("$facet - seven metrics in ONE pass over `books`")
    show("totals", t.client.get("/analytics/dashboard").get_json()["totals"])
    note("without $facet this would be seven separate queries")


def s_transactions(t: Tour):
    """Multi-document ACID transactions and rollback"""
    banner("09", "Multi-document ACID transactions",
           "Module 1 session 4 - ACID vs BASE")

    headers = t.auth("student")
    book = t.db[BOOKS].find_one({"copies.available": {"$gt": 1}})
    book_id = str(book["_id"])
    show("book", book["title"][:52])
    show("available before", book["copies"]["available"])

    step("POST /borrow - two writes, one transaction")
    note("insert into borrow_records AND decrement books.copies.available")
    result = t.client.post(
        "/borrow", json={"user_id": t.user_id, "book_id": book_id}, headers=headers
    ).get_json()
    show("available after", result["book"]["available_after"])
    borrow_id = result["borrow_id"]

    step("POST /borrow/demo-failure - deliberate crash between the two writes")
    failure = t.client.post(
        "/borrow/demo-failure",
        json={"user_id": t.user_id, "book_id": book_id},
        headers=headers,
    ).get_json()
    show("copies before / after",
         f"{failure['copies_available_before']} / {failure['copies_available_after']}")
    show("records before / after",
         f"{failure['borrow_records_before']} / {failure['borrow_records_after']}")
    show("rolled_back", failure["rolled_back"])
    note("the decrement DID happen, then the transaction aborted - nothing survives")
    note("THIS is atomicity: all of it, or none of it")

    step("POST /borrow/return - closes the loan, refreshes Computed Pattern fields")
    returned = t.client.post(
        "/borrow/return", json={"borrow_id": borrow_id, "rating": 5}, headers=headers
    ).get_json()
    show("book after return", returned["book"])


def s_changestreams(t: Tour):
    """Change streams and reactive updates"""
    banner("10", "Change Streams", "Module 3 session 23 - reactive updates")

    note("a change stream reads the replica set's oplog")
    note("the app subscribes instead of polling; events arrive in about a second")

    headers = t.auth("student")
    book = t.db[BOOKS].find_one({"copies.available": {"$gt": 0}})
    book_id = str(book["_id"])

    from app.routes.stream import WATCH_PIPELINE, _describe

    def writer():
        time.sleep(2)
        response = t.client.post(
            "/borrow", json={"user_id": t.user_id, "book_id": book_id}, headers=headers
        ).get_json()
        time.sleep(1)
        t.client.post(
            "/borrow/return",
            json={"borrow_id": response["borrow_id"], "rating": 4},
            headers=headers,
        )

    threading.Thread(target=writer, daemon=True).start()

    step("watching books + borrow_records while a borrow/return happens")
    seen = 0
    with t.db.watch(
        WATCH_PIPELINE, full_document="updateLookup", max_await_time_ms=1000
    ) as stream:
        deadline = time.time() + 20
        while time.time() < deadline and seen < 5:
            change = stream.try_next()
            if change is None:
                continue
            seen += 1
            print(f"      {change['operationType']:8} "
                  f"{change['ns']['coll']:15} {_describe(change)}")

    show("events received", seen)
    note("open /stream/demo in a browser for the same feed live, with no refresh")


def s_security(t: Tour):
    """Password hashing, JWT, role-based access control"""
    banner("11", "Authentication and roles",
           "Module 4 session 28 - users, roles, authentication")

    step("three seeded accounts, three roles")
    for role in ("student", "librarian", "admin"):
        user = t.db[USERS].find_one({"role": role})
        if user:
            print(f"      {role:10} {user['email']}")

    step("passwords are bcrypt hashes, never plaintext")
    user = t.db[USERS].find_one({"role": "student"})
    show("stored value", user["password_hash"][:44] + "...")
    note("the salt is embedded in the hash; no separate salt field is needed")

    step("POST /auth/login returns a JWT")
    body = t.client.post(
        "/auth/login", json={"email": user["email"], "password": SEED_PASSWORD}
    ).get_json()
    show("token", body["access_token"][:44] + "...")
    show("user", body["user"])

    step("wrong password")
    bad = t.client.post(
        "/auth/login", json={"email": user["email"], "password": "wrong-password"}
    )
    show("status", bad.status_code)
    show("message", bad.get_json()["error"])
    note("identical message for an unknown email, so accounts cannot be enumerated")

    step("role matrix")
    print(f"      {'endpoint':34} {'none':>5} {'student':>8} {'librarian':>10} {'admin':>6}")
    probe = {"title": "RBAC probe", "authors": ["a"], "genre": "Fiction",
             "publication_year": 2000, "price": 1,
             "publisher": {"name": "p"}, "copies": {"total": 1, "available": 1}}
    checks = [
        ("GET  /books          (public)", "get", "/books?limit=1", {}),
        ("GET  /borrow         (any auth)", "get", "/borrow?limit=1", {}),
        ("POST /books          (librarian+)", "post", "/books", {"json": probe}),
        ("DELETE /users/<id>   (admin only)", "delete",
         "/users/000000000000000000000000", {}),
    ]
    for label, method, path, kwargs in checks:
        codes = []
        for role in [None, "student", "librarian", "admin"]:
            headers = t.auth(role) if role else {}
            codes.append(
                getattr(t.client, method)(path, headers=headers, **kwargs).status_code
            )
        print(f"      {label:34} {codes[0]:>5} {codes[1]:>8} "
              f"{codes[2]:>10} {codes[3]:>6}")

    removed = t.db[BOOKS].delete_many({"title": "RBAC probe"}).deleted_count
    note(f"cleaned up {removed} probe document(s)")
    note("these are APPLICATION roles carried in the JWT")
    note("the Atlas user has its own DATABASE role (readWrite) - a separate layer")


SECTIONS = [
    ("connection", s_connection),
    ("model", s_model),
    ("validation", s_validation),
    ("patterns", s_patterns),
    ("crud", s_crud),
    ("query", s_query),
    ("indexing", s_indexing),
    ("aggregation", s_aggregation),
    ("transactions", s_transactions),
    ("changestreams", s_changestreams),
    ("security", s_security),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Guided tour of every concept.")
    parser.add_argument("--pause", action="store_true",
                        help="wait for Enter between sections")
    parser.add_argument("--only", help="run one section by key")
    parser.add_argument("--list", action="store_true", help="list section keys")
    args = parser.parse_args()

    if args.list:
        for key, function in SECTIONS:
            print(f"  {key:16} {(function.__doc__ or '').strip()}")
        return

    print("=" * WIDTH)
    print("  BookVault - guided tour")
    print("  AIM3141 NoSQL Database  |  Flask + PyMongo + MongoDB Atlas")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * WIDTH)

    tour = Tour()
    tour.login_all()
    tour.pick_ids()

    if not tour.tokens:
        print("\n  Could not log in. Has the database been seeded?")
        print("  Run: python -m scripts.seed_books --drop")
        close_client()
        return

    selected = [(k, f) for k, f in SECTIONS if not args.only or k == args.only]
    if not selected:
        print(f"\n  No section called '{args.only}'. Keys: {[k for k, _ in SECTIONS]}")
        close_client()
        return

    for _, function in selected:
        function(tour)
        if args.pause and len(selected) > 1:
            input("\n  [Enter] for the next section ")

    print()
    print("=" * WIDTH)
    print("  Tour complete.")
    print("  Supporting evidence:")
    print("    docs/benchmarks.md        python -m scripts.benchmark_index")
    print("    17 validator rejections   python -m scripts.validation_demo")
    print("    live change-stream feed   /stream/demo in a browser")
    print("=" * WIDTH)

    close_client()


if __name__ == "__main__":
    main()
