"""Request validation helpers.

Flask does not validate request bodies, so these do it explicitly. This is
the APPLICATION layer of validation. MongoDB's $jsonSchema validators
(app/core/schema.py) enforce the same rules again inside the DATABASE.

Having both on purpose is a point the report makes: a NoSQL database is not
schema-less, and the database guards its own data even when a write arrives
from mongosh or Compass instead of through this API.
"""

from decimal import Decimal, InvalidOperation

from app.core.errors import ApiError

# Sentinel meaning "the client did not supply this field at all", which is
# different from supplying null. PATCH needs that distinction.
MISSING = object()


def _fail(message: str):
    raise ApiError(400, "validation failed", message)


def require_body(body) -> dict:
    """The JSON body must be present and must be an object."""
    if body is None:
        _fail("request body must be JSON (set Content-Type: application/json)")
    if not isinstance(body, dict):
        _fail("request body must be a JSON object")
    return body


def reject_unknown(data: dict, allowed: set[str]) -> None:
    """Catch typos like 'titel' instead of silently ignoring them."""
    unknown = sorted(set(data) - allowed)
    if unknown:
        _fail(f"unknown field(s): {', '.join(unknown)}; allowed: {sorted(allowed)}")


def field_str(
    data: dict,
    name: str,
    *,
    required: bool = True,
    min_len: int = 1,
    max_len: int | None = None,
):
    value = data.get(name, MISSING)
    if value is MISSING:
        if required:
            _fail(f"'{name}' is required")
        return MISSING
    if not isinstance(value, str):
        _fail(f"'{name}' must be a string")
    if len(value) < min_len:
        _fail(f"'{name}' must be at least {min_len} character(s)")
    if max_len is not None and len(value) > max_len:
        _fail(f"'{name}' must be at most {max_len} characters")
    return value


def field_int(
    data: dict,
    name: str,
    *,
    required: bool = True,
    minimum: int | None = None,
    maximum: int | None = None,
):
    value = data.get(name, MISSING)
    if value is MISSING:
        if required:
            _fail(f"'{name}' is required")
        return MISSING
    # bool is a subclass of int in Python, so exclude it explicitly.
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"'{name}' must be an integer")
    if minimum is not None and value < minimum:
        _fail(f"'{name}' must be >= {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"'{name}' must be <= {maximum}")
    return value


def field_decimal(
    data: dict,
    name: str,
    *,
    required: bool = True,
    minimum: Decimal | None = None,
):
    """Money. Returns a Decimal, which the route stores as BSON Decimal128."""
    value = data.get(name, MISSING)
    if value is MISSING:
        if required:
            _fail(f"'{name}' is required")
        return MISSING
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        _fail(f"'{name}' must be a number")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        _fail(f"'{name}' is not a valid number")
        return MISSING  # unreachable, keeps type checkers quiet
    if minimum is not None and parsed < minimum:
        _fail(f"'{name}' must be >= {minimum}")
    return parsed


def field_str_list(
    data: dict,
    name: str,
    *,
    required: bool = True,
    min_items: int = 0,
):
    value = data.get(name, MISSING)
    if value is MISSING:
        if required:
            _fail(f"'{name}' is required")
        return MISSING
    if not isinstance(value, list):
        _fail(f"'{name}' must be an array")
    if len(value) < min_items:
        _fail(f"'{name}' must contain at least {min_items} item(s)")
    for item in value:
        if not isinstance(item, str) or not item:
            _fail(f"every item in '{name}' must be a non-empty string")
    return value


def field_enum(data: dict, name: str, allowed, *, required: bool = True):
    value = data.get(name, MISSING)
    if value is MISSING:
        if required:
            _fail(f"'{name}' is required")
        return MISSING
    if value not in allowed:
        _fail(f"'{name}' must be one of {list(allowed)}")
    return value


def field_object(data: dict, name: str, *, required: bool = True):
    value = data.get(name, MISSING)
    if value is MISSING:
        if required:
            _fail(f"'{name}' is required")
        return MISSING
    if not isinstance(value, dict):
        _fail(f"'{name}' must be an object")
    return value


# --- query-string helpers (request.args values are always strings) ---


def arg_int(args, name: str, *, default=None, minimum=None, maximum=None):
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        _fail(f"query parameter '{name}' must be an integer")
        return default  # unreachable
    if minimum is not None and value < minimum:
        _fail(f"query parameter '{name}' must be >= {minimum}")
    if maximum is not None and value > maximum:
        _fail(f"query parameter '{name}' must be <= {maximum}")
    return value


def arg_bool(args, name: str, *, default: bool = False) -> bool:
    raw = args.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
