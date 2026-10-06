"""MongoDB connection management.

A single MongoClient is shared across the process. PyMongo's client is
thread-safe and maintains its own connection pool, so creating one per
request would be wasteful and would exhaust the Atlas M0 connection limit.
"""

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.server_api import ServerApi

from app.config import get_settings

# Collection names kept in one place so scripts and routers cannot drift.
BOOKS = "books"
USERS = "users"
BORROW_RECORDS = "borrow_records"
REVIEWS_OVERFLOW = "reviews_overflow"

ALL_COLLECTIONS = (BOOKS, USERS, BORROW_RECORDS, REVIEWS_OVERFLOW)

_client: MongoClient | None = None


def get_client() -> MongoClient:
    global _client
    if _client is None:
        settings = get_settings()
        _client = MongoClient(
            settings.mongodb_uri,
            server_api=ServerApi("1"),
            tz_aware=True,
            appname="bookvault",
            serverSelectionTimeoutMS=10_000,
        )
    return _client


def get_db() -> Database:
    return get_client()[get_settings().db_name]


def collection(name: str) -> Collection:
    return get_db()[name]


def ping() -> dict:
    """Round-trip to the server. Used by the health check and setup scripts."""
    return get_client().admin.command("ping")


def close_client() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
