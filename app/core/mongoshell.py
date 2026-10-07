"""Show the MongoDB operations a request performs, as mongosh text.

The Developer view displays the actual query behind every screen. The text is
GENERATED from the same filter, projection, sort and document objects that are
passed to PyMongo, never typed out separately, so it cannot drift away from
what really ran.

How it works:
  - A route calls record(label, code) just before it executes an operation.
  - An after_request hook sends the collected operations back in the response
    header X-MongoDB-Operations, as a JSON list of {label, code}.
  - The JSON body of every endpoint is unchanged.

Recording happens BEFORE the operation runs, so a write that MongoDB rejects
(for example with a $jsonSchema violation) still shows the attempted query.
"""

import json
import re
from datetime import datetime, timezone
from decimal import Decimal

from bson import Decimal128, ObjectId
from flask import g, has_request_context

HEADER = "X-MongoDB-Operations"

# Keep the header comfortably below typical proxy limits.
MAX_HEADER_BYTES = 7000

_PLAIN_KEY = re.compile(r"^\$?[A-Za-z_][A-Za-z0-9_]*$")
_WIDTH = 76


def _key(name: str) -> str:
    # Operators and plain identifiers are bare in mongosh. Dotted paths such
    # as "publisher.country" must be quoted.
    return name if _PLAIN_KEY.match(name) else json.dumps(name)


def shell(value, level: int = 0) -> str:
    """Render a Python/BSON value the way mongosh would print it."""
    pad = "  " * level
    inner = "  " * (level + 1)

    if isinstance(value, dict):
        if not value:
            return "{}"
        items = [f"{_key(str(k))}: {shell(v, level + 1)}" for k, v in value.items()]
        flat = "{ " + ", ".join(items) + " }"
        if len(flat) <= _WIDTH and "\n" not in flat:
            return flat
        return "{\n" + ",\n".join(inner + i for i in items) + "\n" + pad + "}"

    if isinstance(value, (list, tuple)):
        if not value:
            return "[]"
        items = [shell(v, level + 1) for v in value]
        flat = "[ " + ", ".join(items) + " ]"
        if len(flat) <= _WIDTH and "\n" not in flat:
            return flat
        return "[\n" + ",\n".join(inner + i for i in items) + "\n" + pad + "]"

    if isinstance(value, ObjectId):
        return f'ObjectId("{value}")'
    if isinstance(value, Decimal128):
        return f'NumberDecimal("{value}")'
    if isinstance(value, Decimal):
        return f'NumberDecimal("{value}")'
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        text = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        return f'ISODate("{text}Z")'
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value), ensure_ascii=False)


def sort_doc(sort) -> dict:
    """PyMongo sorts are lists of (field, direction); mongosh wants a document."""
    if isinstance(sort, dict):
        return sort
    return {field: direction for field, direction in sort}


def call(collection: str, method: str, *args, prefix: str = "db") -> str:
    """db.collection.method(arg, arg), wrapped over lines only when long."""
    rendered = [shell(a) for a in args]
    head = f"{prefix}.{collection}.{method}("
    flat = head + ", ".join(rendered) + ")"
    if len(flat) <= _WIDTH and "\n" not in flat:
        return flat
    body = ",\n".join("  " + r.replace("\n", "\n  ") for r in rendered)
    return f"{head}\n{body}\n)"


def find_call(
    collection: str,
    query: dict,
    projection=None,
    sort=None,
    skip=None,
    limit=None,
    explain: bool = False,
    prefix: str = "db",
    hint=None,
) -> str:
    args = [query] if projection is None else [query, projection]
    text = call(collection, "find", *args, prefix=prefix)
    if hint:
        text += f"\n  .hint({shell(sort_doc(hint))})"
    if sort:
        text += f"\n  .sort({shell(sort_doc(sort))})"
    if skip:
        text += f"\n  .skip({skip})"
    if limit:
        text += f"\n  .limit({limit})"
    if explain:
        text += '\n  .explain("executionStats")'
    return text


def agg_call(collection: str, pipeline: list, prefix: str = "db") -> str:
    return call(collection, "aggregate", pipeline, prefix=prefix)


def redact(document: dict, *fields: str) -> dict:
    """A copy with secret fields replaced, so a hash is never displayed."""
    copy = dict(document)
    for field in fields:
        if field in copy:
            copy[field] = "<redacted>"
    return copy


def record(label: str, code: str) -> None:
    if not has_request_context():
        return
    if not hasattr(g, "mongo_ops"):
        g.mongo_ops = []
    g.mongo_ops.append({"label": label, "code": code})


def attach(response):
    """after_request hook: send the recorded operations to the browser."""
    ops = getattr(g, "mongo_ops", None) if has_request_context() else None
    if not ops:
        return response

    payload = json.dumps(ops, ensure_ascii=True, separators=(",", ":"))
    if len(payload.encode("ascii")) > MAX_HEADER_BYTES:
        trimmed = [
            {"label": op["label"], "code": op["code"][:900] + "\n// ... shortened for the header"}
            for op in ops
        ]
        payload = json.dumps(trimmed, ensure_ascii=True, separators=(",", ":"))

    response.headers[HEADER] = payload
    # Lets a page served from another origin read the header during development.
    response.headers["Access-Control-Expose-Headers"] = HEADER
    return response
