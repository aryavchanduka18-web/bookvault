"""Change Streams - reactive updates (Module 3, session 23).

MongoDB change streams let an application subscribe to writes as they
happen instead of polling the collection. Internally they read the replica
set's oplog, which is why a change stream only works against a replica set
(Atlas M0 is one) and not a standalone mongod.

The browser side uses Server-Sent Events: one long-lived HTTP response the
server keeps writing to. SSE rather than WebSockets because the traffic is
one-directional - the server pushes, the page only listens.

    MongoDB write -> oplog -> change stream -> Flask generator -> EventSource
"""

import json

from flask import Blueprint, Response, jsonify

from app.core.serializers import jsonable
from app.db import BOOKS, BORROW_RECORDS, get_db

bp = Blueprint("stream", __name__, url_prefix="/stream")

# Only react to writes on the two collections the dashboard cares about.
# Filtering inside the pipeline means the server never ships events the
# client would just discard.
WATCH_PIPELINE = [
    {
        "$match": {
            "ns.coll": {"$in": [BOOKS, BORROW_RECORDS]},
            "operationType": {"$in": ["insert", "update", "replace", "delete"]},
        }
    }
]

# How long watch() blocks waiting for a change before the generator loops
# round and sends a keepalive comment.
AWAIT_MS = 1000


def _describe(change: dict) -> str:
    """Turn a raw change event into a line a human can read."""
    operation = change.get("operationType")
    collection_name = (change.get("ns") or {}).get("coll")
    document = change.get("fullDocument") or {}

    if collection_name == BORROW_RECORDS:
        title = (document.get("book") or {}).get("title", "a book")
        if operation == "insert":
            return f"Borrowed - {title}"
        if operation in ("update", "replace"):
            if document.get("status") == "returned":
                rating = document.get("rating")
                return f"Returned - {title}" + (f" (rated {rating}/5)" if rating else "")
            return f"Loan updated - {title}"
        if operation == "delete":
            return "Borrow record deleted"

    if collection_name == BOOKS:
        title = document.get("title", "a book")
        if operation == "insert":
            return f"New book added - {title}"
        if operation in ("update", "replace"):
            available = (document.get("copies") or {}).get("available")
            if available is not None:
                return f"Stock changed - {title} ({available} available)"
            return f"Book updated - {title}"
        if operation == "delete":
            return "Book deleted"

    return f"{operation} on {collection_name}"


def _event_stream():
    """Generator yielding SSE frames for the lifetime of the connection."""
    db = get_db()

    # full_document="updateLookup" makes MongoDB attach the whole document
    # after an update. Without it an update event carries only the changed
    # fields, so there would be no title to display.
    with db.watch(
        WATCH_PIPELINE,
        full_document="updateLookup",
        max_await_time_ms=AWAIT_MS,
    ) as stream:

        yield 'event: ready\ndata: {"message": "watching books and borrow_records"}\n\n'

        while True:
            change = stream.try_next()

            if change is None:
                # No write in the last AWAIT_MS. An SSE comment keeps the
                # connection (and any proxy in front of it) alive.
                yield ": keepalive\n\n"
                continue

            payload = {
                "operation": change.get("operationType"),
                "collection": (change.get("ns") or {}).get("coll"),
                "description": _describe(change),
                "document_id": jsonable((change.get("documentKey") or {}).get("_id")),
                "cluster_time": str(change.get("clusterTime")),
            }

            updated = (change.get("updateDescription") or {}).get("updatedFields")
            if updated:
                payload["updated_fields"] = sorted(updated)

            yield f"event: change\ndata: {json.dumps(payload)}\n\n"


@bp.get("/events")
def events():
    """Live event feed. Consume with `new EventSource('/stream/events')`."""
    return Response(
        _event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stops nginx-style proxies buffering the stream.
            "X-Accel-Buffering": "no",
        },
    )


@bp.get("/info")
def info():
    """What this endpoint watches - handy during a demo."""
    return jsonify(
        {
            "watching": [BOOKS, BORROW_RECORDS],
            "operations": ["insert", "update", "replace", "delete"],
            "pipeline": WATCH_PIPELINE,
            "transport": "Server-Sent Events (text/event-stream)",
            "requires": "a replica set - change streams read the oplog",
            "try_it": "open /stream/demo, then POST /borrow from another window",
        }
    )


DEMO_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BookVault - Live Activity</title>
<style>
  body { font-family: ui-monospace, Consolas, monospace; margin: 0;
         padding: 24px; background: #0f1115; color: #e6e6e6; }
  h1 { font-size: 18px; margin: 0 0 4px; }
  p.sub { margin: 0 0 20px; color: #8b93a7; font-size: 13px; }
  #status { display: inline-block; padding: 3px 10px; border-radius: 999px;
            font-size: 12px; background: #3a2a2a; color: #ff8f8f; }
  #status.live { background: #16301f; color: #58d68d; }
  ul { list-style: none; padding: 0; margin: 20px 0 0; }
  li { padding: 10px 14px; margin-bottom: 6px; border-radius: 6px;
       background: #171a21; border-left: 3px solid #2d6cdf;
       animation: in .25s ease-out; }
  li .op { color: #58a6ff; }
  li .coll { color: #d2a8ff; }
  li .time { float: right; color: #6e7681; font-size: 12px; }
  @keyframes in { from { opacity: 0; transform: translateY(-6px); } }
  .empty { color: #6e7681; border-left-color: #30363d; }
</style>
</head>
<body>
  <h1>BookVault &mdash; Live Activity</h1>
  <p class="sub">MongoDB Change Stream &rarr; Flask SSE &rarr; this page. No polling, no refresh.</p>
  <span id="status">connecting&hellip;</span>
  <ul id="feed"><li class="empty">Waiting for a database write&hellip;
      try <code>POST /borrow</code> in Postman.</li></ul>

<script>
  const feed = document.getElementById("feed");
  const status = document.getElementById("status");
  const source = new EventSource("/stream/events");

  source.addEventListener("ready", () => {
    status.textContent = "live";
    status.className = "live";
  });

  source.addEventListener("change", (event) => {
    const data = JSON.parse(event.data);
    const empty = feed.querySelector(".empty");
    if (empty) empty.remove();

    const item = document.createElement("li");
    item.innerHTML =
      '<span class="time">' + new Date().toLocaleTimeString() + '</span>' +
      '<strong>' + data.description + '</strong><br>' +
      '<span class="op">' + data.operation + '</span> on ' +
      '<span class="coll">' + data.collection + '</span>' +
      (data.updated_fields ? ' &mdash; ' + data.updated_fields.join(", ") : "");
    feed.prepend(item);

    while (feed.children.length > 40) feed.lastChild.remove();
  });

  source.onerror = () => {
    status.textContent = "disconnected - retrying";
    status.className = "";
  };
</script>
</body>
</html>
"""


@bp.get("/demo")
def demo():
    """A standalone page proving the change stream works end to end."""
    return Response(DEMO_PAGE, mimetype="text/html")
