"""Seed the database with realistic data.

Books come from the Open Library public API (openlibrary.org) - real titles,
real authors, real publication years, real publishers, real subjects.
Library-specific fields that Open Library does not have (price, number of
copies, borrowers, ratings) are synthesized.

Be honest about that split in the report:

    REAL       title, authors, publication_year, publisher.name, tags
    DERIVED    genre          (mapped from Open Library subjects)
    SYNTHETIC  price, copies, publisher.country, users, borrow_records, ratings

Volume is the point. Index benchmarks on 20 documents show nothing, because
MongoDB scans 20 documents instantly whether an index exists or not.

Run:
    python -m scripts.seed_books                  # ~2000 books
    python -m scripts.seed_books --books 500      # smaller/faster
    python -m scripts.seed_books --drop           # wipe first, then reseed
    python -m scripts.seed_books --offline        # no network, synthetic titles
"""

import argparse
import random
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
from bson import Decimal128
from pymongo import UpdateOne
from pymongo.errors import BulkWriteError

from app.core.schema import GENRES, MEMBERSHIPS, OUTLIER_REVIEW_THRESHOLD
from app.core.security import hash_password
from app.db import (
    BOOKS,
    BORROW_RECORDS,
    REVIEWS_OVERFLOW,
    USERS,
    close_client,
    get_db,
)

OPEN_LIBRARY = "https://openlibrary.org/search.json"
USER_AGENT = "BookVault-CollegeProject/1.0 (AIM3141 NoSQL coursework)"
PAGE_SIZE = 100
REQUEST_PAUSE = 0.4  # be polite to a free public API

DEFAULT_BOOK_TARGET = 2000
USER_COUNT = 200
BORROW_COUNT = 1500
OUTLIER_BOOKS = 5

# Deterministic output, so a reseed produces the same database.
rng = random.Random(3141)

# Hash once - bcrypt is deliberately slow, so hashing 200 times would add
# roughly a minute to the seed for no benefit.
SEED_PASSWORD = "bookvault123"
SEED_PASSWORD_HASH = hash_password(SEED_PASSWORD)


# --------------------------------------------------------------------------
# Open Library
# --------------------------------------------------------------------------

# Each genre is filled from several searches, otherwise one query's results
# dominate and every book in that genre looks the same.
GENRE_QUERIES = {
    "Programming": ["programming", "software engineering", "python programming",
                    "javascript", "algorithms", "clean code"],
    "Fiction": ["fiction", "novel", "short stories", "mystery novel", "science fiction"],
    "Science": ["physics", "biology", "chemistry", "astronomy", "neuroscience"],
    "History": ["world history", "ancient history", "medieval history", "indian history"],
    "Mathematics": ["mathematics", "calculus", "linear algebra", "probability", "geometry"],
    "Philosophy": ["philosophy", "ethics", "logic", "stoicism", "political philosophy"],
    "Biography": ["biography", "memoir", "autobiography", "life story"],
    "Technology": ["technology", "artificial intelligence", "computer networks",
                   "robotics", "databases"],
    "Poetry": ["poetry", "poems", "collected poems", "verse"],
    "Reference": ["dictionary", "encyclopedia", "handbook", "manual", "atlas"],
}

FIELDS = "title,author_name,first_publish_year,publisher,subject,number_of_pages_median"

# Open Library has no country data, so publisher country is synthesized.
# Known imprints are mapped; everything else is drawn at random.
PUBLISHER_COUNTRY = {
    "penguin": "UK", "penguin books": "UK", "oxford university press": "UK",
    "cambridge university press": "UK", "bloomsbury": "UK", "macmillan": "UK",
    "prentice hall": "USA", "pearson": "USA", "o'reilly media": "USA",
    "addison-wesley": "USA", "mcgraw-hill": "USA", "wiley": "USA",
    "random house": "USA", "harpercollins": "USA", "simon & schuster": "USA",
    "springer": "Germany", "de gruyter": "Germany",
    "rupa publications": "India", "jaico publishing house": "India",
    "s. chand": "India", "pearson education india": "India",
}
FALLBACK_COUNTRIES = ["USA", "UK", "India", "Germany", "Canada", "Australia"]


def fetch_genre(genre: str, wanted: int) -> list[dict]:
    """Pull raw Open Library records for one genre."""
    collected: list[dict] = []

    for query in GENRE_QUERIES[genre]:
        if len(collected) >= wanted:
            break
        page = 1
        while len(collected) < wanted and page <= 3:
            try:
                response = requests.get(
                    OPEN_LIBRARY,
                    params={"q": query, "fields": FIELDS, "limit": PAGE_SIZE, "page": page},
                    headers={"User-Agent": USER_AGENT},
                    timeout=30,
                )
                response.raise_for_status()
                docs = response.json().get("docs", [])
            except Exception as exc:
                print(f"    ! {genre}/{query} page {page} failed: {exc}")
                break

            if not docs:
                break

            collected.extend(docs)
            page += 1
            time.sleep(REQUEST_PAUSE)

    return collected[:wanted]


def to_book(raw: dict, genre: str) -> dict | None:
    """Map one Open Library record onto the books schema.

    Returns None for records missing anything the $jsonSchema validator
    requires - Open Library data is real, which means it is also messy.
    """
    title = (raw.get("title") or "").strip()
    authors = [a.strip() for a in (raw.get("author_name") or []) if a and a.strip()]
    year = raw.get("first_publish_year")
    publishers = [p.strip() for p in (raw.get("publisher") or []) if p and p.strip()]

    if not title or not authors or not isinstance(year, int):
        return None
    if not (1450 <= year <= 2100):
        return None

    title = title[:300]
    authors = authors[:4]

    publisher_name = (publishers[0] if publishers else "Unknown Press")[:200]
    country = PUBLISHER_COUNTRY.get(publisher_name.lower(), rng.choice(FALLBACK_COUNTRIES))

    # tags: lowercase, de-duplicated, short. These feed $unwind in the
    # popular_tags pipeline and the text index.
    tags: list[str] = []
    for subject in (raw.get("subject") or []):
        clean = str(subject).strip().lower()
        if clean and len(clean) <= 40 and clean not in tags:
            tags.append(clean)
        if len(tags) == 6:
            break

    # SYNTHETIC: price loosely tracks page count, so the $bucket distribution
    # is shaped rather than uniform noise.
    pages = raw.get("number_of_pages_median") or rng.randint(120, 600)
    price = round(min(max(150 + pages * rng.uniform(0.8, 2.2), 99), 4800), 2)

    total = rng.choice([1, 2, 2, 3, 3, 4, 5, 6, 8])

    return {
        "title": title,
        "authors": authors,
        "genre": genre,
        "publication_year": int(year),
        "price": Decimal128(str(price)),
        "publisher": {"name": publisher_name, "country": country},
        # available starts equal to total; open loans decrement it later so
        # stock always matches borrow_records.
        "copies": {"total": total, "available": total},
        "tags": tags,
        "avg_rating": None,
        "borrow_count": 0,
        "review_count": 0,
        "has_many_reviews": False,
        "added_at": datetime.now(timezone.utc) - timedelta(days=rng.randint(0, 730)),
    }


# --------------------------------------------------------------------------
# offline fallback
# --------------------------------------------------------------------------

OFFLINE_NOUNS = ["Systems", "Patterns", "Foundations", "Structures", "Methods",
                 "Principles", "Algorithms", "Models", "Theory", "Practice"]
OFFLINE_ADJS = ["Modern", "Practical", "Advanced", "Essential", "Applied",
                "Concise", "Complete", "Distributed", "Concurrent", "Elementary"]
OFFLINE_SURNAMES = ["Martin", "Kleppmann", "Knuth", "Hopper", "Lovelace", "Dijkstra",
                    "Liskov", "Torvalds", "Ritchie", "Iyer", "Sharma", "Banerjee"]
OFFLINE_FIRST = ["Robert", "Martin", "Donald", "Grace", "Ada", "Edsger",
                 "Barbara", "Linus", "Dennis", "Priya", "Arun", "Meera"]


def generate_offline(genre: str, count: int) -> list[dict]:
    """Synthetic books, used only when Open Library is unreachable."""
    out = []
    for index in range(count):
        raw = {
            "title": (
                f"{rng.choice(OFFLINE_ADJS)} {genre} {rng.choice(OFFLINE_NOUNS)} "
                f"Vol. {index + 1}"
            ),
            "author_name": [f"{rng.choice(OFFLINE_FIRST)} {rng.choice(OFFLINE_SURNAMES)}"],
            "first_publish_year": rng.randint(1960, 2025),
            "publisher": [rng.choice(list(PUBLISHER_COUNTRY)).title()],
            "subject": rng.sample(
                ["reference", "textbook", "university", "research", "introductory",
                 "advanced", "classic"], k=3
            ),
            "number_of_pages_median": rng.randint(120, 800),
        }
        book = to_book(raw, genre)
        if book:
            out.append(book)
    return out


# --------------------------------------------------------------------------
# users
# --------------------------------------------------------------------------

FIRST_NAMES = ["Aryav", "Ananya", "Rohan", "Ishita", "Kabir", "Meera", "Arjun", "Diya",
               "Vivaan", "Sara", "Aditya", "Nisha", "Karan", "Tanvi", "Rahul", "Priya",
               "Siddharth", "Riya", "Manan", "Aisha", "Dev", "Neha", "Yash", "Pooja"]
LAST_NAMES = ["Chanduka", "Sharma", "Verma", "Iyer", "Banerjee", "Kapoor", "Reddy",
              "Nair", "Mehta", "Chauhan", "Gupta", "Joshi", "Rao", "Singh", "Desai"]
DEPARTMENTS = ["CSE", "AIML", "ECE", "Mechanical", "Civil", "Design", "Management",
               "Mathematics", "Physics"]


def generate_users(count: int) -> list[dict]:
    users = []
    for index in range(count):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)

        # index keeps the address unique, which the unique index demands.
        email = f"{first.lower()}.{last.lower()}{index}@muj.manipal.edu"

        role = "admin" if index == 0 else ("librarian" if index < 6 else "student")

        users.append({
            "name": f"{first} {last}",
            "email": email,
            "department": rng.choice(DEPARTMENTS),
            "membership": rng.choices(MEMBERSHIPS, weights=[80, 15, 5])[0],
            "role": role,
            # Every seeded account shares one well-known demo password. Fine
            # for a throwaway coursework dataset; never do this for real.
            "password_hash": SEED_PASSWORD_HASH,
            "joined_at": datetime.now(timezone.utc) - timedelta(days=rng.randint(30, 900)),
        })
    return users


# --------------------------------------------------------------------------
# borrow records
# --------------------------------------------------------------------------

REVIEW_TEXTS = [
    "Clear explanations and well paced.",
    "Dense in places but worth finishing.",
    "Great reference, less good as a first read.",
    "Exactly what I needed for the course.",
    "Dated in parts, still useful.",
    "Would recommend to anyone starting out.",
    "Too theoretical for my taste.",
    "The examples alone justify it.",
]


def build_borrow_records(books, users, count, outlier_ids):
    """Create borrow records and the stock changes they imply.

    books.copies.available is kept consistent with the number of records
    left open, so the seeded data could plausibly have been produced by the
    /borrow endpoint rather than invented.
    """
    records = []
    stock = {b["_id"]: dict(b["copies"]) for b in books}
    borrow_counts = {b["_id"]: 0 for b in books}
    ratings: dict = {b["_id"]: [] for b in books}
    overflow: dict = {oid: [] for oid in outlier_ids}

    now = datetime.now(timezone.utc)
    by_id = {b["_id"]: b for b in books}

    # Outlier books get enough rated loans to push review_count past the
    # threshold, which is what makes has_many_reviews true.
    plan = []
    for book_id in outlier_ids:
        plan.extend([book_id] * (OUTLIER_REVIEW_THRESHOLD + rng.randint(12, 25)))
    plan.extend(rng.choice(books)["_id"] for _ in range(max(count - len(plan), 0)))
    rng.shuffle(plan)

    outlier_set = set(outlier_ids)

    for book_id in plan:
        book = by_id[book_id]
        user = rng.choice(users)

        # Outlier books must finish with MORE than OUTLIER_REVIEW_THRESHOLD
        # ratings, so their loans are always returned and always rated.
        # Leaving it to chance yields ~30 ratings, under the threshold, and
        # has_many_reviews never becomes true.
        is_outlier = book_id in outlier_set

        # 75% returned. An open loan is only possible if a copy is free.
        returned = True if is_outlier else rng.random() < 0.75
        if not returned and stock[book_id]["available"] <= 0:
            returned = True

        # Open loans are recent, so only a minority are overdue - spreading
        # them over a whole year would make ~95% overdue, which is not what
        # a real library looks like. Returned loans do span the year, to
        # give $dateToString meaningful monthly buckets.
        age_days = rng.randint(1, 365) if returned else rng.randint(1, 24)
        borrowed_at = now - timedelta(days=age_days, hours=rng.randint(0, 23))

        record = {
            "user_id": user["_id"],
            # Extended Reference Pattern: snapshot of the book at borrow time.
            "book": {
                "book_id": book_id,
                "title": book["title"],
                "authors": book["authors"],
            },
            "borrowed_at": borrowed_at,
            "due_date": borrowed_at + timedelta(days=14),
            "returned_at": None,
            "status": "borrowed",
            "rating": None,
        }

        if returned:
            record["returned_at"] = borrowed_at + timedelta(days=rng.randint(2, 30))
            record["status"] = "returned"

            if is_outlier or rng.random() < 0.65:
                rating = rng.choices([1, 2, 3, 4, 5], weights=[3, 7, 20, 40, 30])[0]
                record["rating"] = rating
                ratings[book_id].append(rating)

                if book_id in overflow:
                    overflow[book_id].append({
                        "user_id": user["_id"],
                        "rating": rating,
                        "text": rng.choice(REVIEW_TEXTS),
                        "created_at": record["returned_at"],
                    })
        else:
            # Still on loan, so a copy is off the shelf. "Overdue" is not a
            # stored status - it is derived at query time from due_date.
            stock[book_id]["available"] -= 1

        borrow_counts[book_id] += 1
        records.append(record)

    return records, stock, borrow_counts, ratings, overflow


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def insert_batched(coll, docs, label, size=500):
    inserted = 0
    for start in range(0, len(docs), size):
        chunk = docs[start:start + size]
        try:
            inserted += len(coll.insert_many(chunk, ordered=False).inserted_ids)
        except BulkWriteError as exc:
            # Documents rejected by the $jsonSchema validator land here.
            inserted += exc.details.get("nInserted", 0)
            errors = exc.details.get("writeErrors", [])
            print(f"\n    ! {len(errors)} document(s) rejected by the validator")
            if errors:
                print(f"      first reason: {str(errors[0].get('errmsg', ''))[:160]}")
        print(f"    {label}: {inserted}/{len(docs)}", end="\r")
    print(f"    {label}: {inserted}/{len(docs)}        ")
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed BookVault with realistic data.")
    parser.add_argument("--books", type=int, default=DEFAULT_BOOK_TARGET)
    parser.add_argument("--users", type=int, default=USER_COUNT)
    parser.add_argument("--borrows", type=int, default=BORROW_COUNT)
    parser.add_argument("--drop", action="store_true", help="empty the collections first")
    parser.add_argument("--offline", action="store_true", help="skip the Open Library API")
    args = parser.parse_args()

    db = get_db()

    if args.drop:
        print("dropping existing documents")
        for name in (BOOKS, USERS, BORROW_RECORDS, REVIEWS_OVERFLOW):
            print(f"    {name:18} {db[name].delete_many({}).deleted_count} removed")
    elif db[BOOKS].count_documents({}, limit=1):
        print("books already contains documents. Re-run with --drop to replace them.")
        close_client()
        sys.exit(1)

    # ---- books -----------------------------------------------------------
    per_genre = max(args.books // len(GENRES), 1)
    print(f"\nfetching ~{per_genre} books per genre ({len(GENRES)} genres)")

    raw_books: list[dict] = []
    seen: set[tuple] = set()

    for genre in GENRES:
        if args.offline:
            candidates = generate_offline(genre, per_genre)
        else:
            candidates = [
                book
                for raw in fetch_genre(genre, per_genre * 2)
                if (book := to_book(raw, genre)) is not None
            ]
            if not candidates:
                print(f"    {genre}: API gave nothing usable, generating offline")
                candidates = generate_offline(genre, per_genre)

        kept = 0
        for book in candidates:
            key = (book["title"].lower(), book["authors"][0].lower())
            if key in seen:
                continue
            seen.add(key)
            raw_books.append(book)
            kept += 1
            if kept >= per_genre:
                break
        print(f"    {genre:14} {kept}")

    print(f"\ninserting {len(raw_books)} books")
    insert_batched(db[BOOKS], raw_books, "books")

    books = list(db[BOOKS].find({}, {"title": 1, "authors": 1, "copies": 1}))
    if not books:
        print("no books were inserted - stopping")
        close_client()
        sys.exit(1)

    # ---- users -----------------------------------------------------------
    print(f"\ninserting {args.users} users")
    insert_batched(db[USERS], generate_users(args.users), "users")
    users = list(db[USERS].find({}, {"_id": 1}))

    # ---- borrow records --------------------------------------------------
    outlier_ids = [b["_id"] for b in rng.sample(books, min(OUTLIER_BOOKS, len(books)))]

    print(f"\nbuilding borrow records (target {args.borrows})")
    records, stock, borrow_counts, ratings, overflow = build_borrow_records(
        books, users, args.borrows, outlier_ids
    )
    insert_batched(db[BORROW_RECORDS], records, "borrow_records")

    # ---- Computed Pattern backfill ---------------------------------------
    print("\nbackfilling Computed Pattern fields on books")
    operations = []
    for book in books:
        book_id = book["_id"]
        book_ratings = ratings[book_id]
        count = len(book_ratings)
        operations.append(UpdateOne(
            {"_id": book_id},
            {"$set": {
                "copies.available": stock[book_id]["available"],
                "borrow_count": borrow_counts[book_id],
                "review_count": count,
                "avg_rating": round(sum(book_ratings) / count, 2) if count else None,
                "has_many_reviews": count > OUTLIER_REVIEW_THRESHOLD,
            }},
        ))

    for start in range(0, len(operations), 500):
        db[BOOKS].bulk_write(operations[start:start + 500], ordered=False)
    print(f"    updated {len(operations)} books")

    # ---- Outlier Pattern -------------------------------------------------
    buckets = [
        {"book_id": book_id, "reviews": reviews}
        for book_id, reviews in overflow.items()
        if reviews
    ]
    if buckets:
        print(f"\nwriting {len(buckets)} overflow buckets (Outlier Pattern)")
        insert_batched(db[REVIEWS_OVERFLOW], buckets, "reviews_overflow")

    # ---- summary ---------------------------------------------------------
    print("\n" + "=" * 58)
    for name in (BOOKS, USERS, BORROW_RECORDS, REVIEWS_OVERFLOW):
        print(f"  {name:18} {db[name].count_documents({}):>6} documents")

    print(f"\n  outlier books       {db[BOOKS].count_documents({'has_many_reviews': True}):>6}")
    print(f"  open loans          {db[BORROW_RECORDS].count_documents({'status': 'borrowed'}):>6}")
    print(
        "  overdue             "
        f"{db[BORROW_RECORDS].count_documents({'status': 'borrowed', 'due_date': {'$lt': datetime.now(timezone.utc)}}):>6}"
    )
    print("=" * 58)
    print(f"\nseed password for every generated user: {SEED_PASSWORD}")

    close_client()


if __name__ == "__main__":
    main()
