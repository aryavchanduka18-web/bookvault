"""BSON to JSON conversion.

Flask's jsonify cannot serialise ObjectId or Decimal128, so every document
leaving a route passes through doc_out().
"""

from datetime import datetime

from bson import Decimal128, ObjectId

from app.core.errors import ApiError


def jsonable(value):
    """Recursively convert BSON types into JSON-safe Python types."""
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    return value


def doc_out(doc: dict | None) -> dict | None:
    """Convert a raw MongoDB document into an API response body."""
    if doc is None:
        return None
    out = jsonable(doc)
    if "_id" in out:
        out["id"] = out.pop("_id")
    return out


def to_object_id(value: str, field: str = "id") -> ObjectId:
    """Parse a URL/query parameter into an ObjectId, or raise HTTP 400."""
    try:
        return ObjectId(value)
    except Exception:
        raise ApiError(400, f"'{value}' is not a valid {field}")
