"""BookVault - Flask application entrypoint.

Run:  flask --app app.main run --debug --port 8000
  or: python -m app.main
"""

from flask import Flask, jsonify
from flask_cors import CORS

from app.config import get_settings
from app.core.errors import register_error_handlers
from app.core.schema import GENRES
from app.core.serializers import jsonable
from app.db import ALL_COLLECTIONS, USERS, collection, get_client, get_db, ping
from app.routes import analytics, auth, books, borrow, stream, users


def create_app() -> Flask:
    # static_folder resolves to app/static, which holds the single-page frontend.
    flask_app = Flask(__name__, static_folder="static", static_url_path="/static")

    # The frontend is served as static files from a different origin during
    # development, so it needs CORS.
    CORS(flask_app)

    register_error_handlers(flask_app)

    for blueprint in (auth.bp, books.bp, users.bp, borrow.bp, analytics.bp, stream.bp):
        flask_app.register_blueprint(blueprint)

    @flask_app.get("/health")
    def health():
        """Liveness probe plus the deployment topology, for the report."""
        client = get_client()
        ping()
        return jsonify(
            {
                "status": "ok",
                "database": get_settings().db_name,
                "topology": client.topology_description.topology_type_name,
                "nodes": [f"{host}:{port}" for host, port in client.nodes],
            }
        )

    @flask_app.get("/")
    def index():
        """The frontend."""
        return flask_app.send_static_file("index.html")

    @flask_app.get("/api")
    def api_index():
        """Route listing, so the API is browsable without Swagger."""
        routes = []
        for rule in flask_app.url_map.iter_rules():
            if rule.endpoint == "static":
                continue
            routes.append(
                {
                    "path": str(rule),
                    "methods": sorted(rule.methods - {"HEAD", "OPTIONS"}),
                }
            )
        routes.sort(key=lambda item: item["path"])
        return jsonify({"service": "BookVault", "routes": routes})

    @flask_app.get("/api/demo-accounts")
    def demo_accounts():
        """One account per role, so the frontend's sign-in box can be populated.

        Returns names and emails only - never password hashes. The demo
        password is the one every seeded user shares, printed by
        scripts/seed_books.py.
        """
        accounts = []
        for role in ("admin", "librarian", "student"):
            user = collection(USERS).find_one(
                {"role": role}, {"name": 1, "email": 1, "role": 1}
            )
            if user:
                accounts.append(
                    {"name": user["name"], "email": user["email"], "role": user["role"]}
                )

        return jsonify({"accounts": accounts, "genres": GENRES})

    @flask_app.get("/api/indexes")
    def index_catalogue():
        """Every index in the database, read from MongoDB itself.

        Read live rather than printed from app/core/indexes.py, so this shows
        what the server actually has, not what the code intended to create.
        """
        db = get_db()
        collections = {}

        for name in ALL_COLLECTIONS:
            entries = []
            for index_name, spec in db[name].index_information().items():
                entry = {
                    "name": index_name,
                    "keys": [[field, kind] for field, kind in spec.get("key", [])],
                }
                if spec.get("unique"):
                    entry["unique"] = True
                if spec.get("partialFilterExpression"):
                    entry["partial_filter"] = jsonable(spec["partialFilterExpression"])
                if spec.get("weights"):
                    entry["text_weights"] = dict(spec["weights"])
                entries.append(entry)
            collections[name] = entries

        return jsonify(
            {
                "collections": collections,
                "total": sum(len(v) for v in collections.values()),
                "note": (
                    "_id_ is created automatically by MongoDB on every "
                    "collection. The rest are declared in app/core/indexes.py."
                ),
            }
        )

    return flask_app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, port=8000)
