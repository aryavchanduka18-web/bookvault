"""Verify the Atlas connection and report the replica-set topology.

Run:  python -m scripts.check_connection
"""

from app.db import close_client, get_client, get_db, ping


def main() -> None:
    client = get_client()

    ping()
    print("connection: ok")

    info = client.server_info()
    print(f"server version: {info['version']}")
    print(f"topology: {client.topology_description.topology_type_name}")
    print(f"nodes: {[f'{h}:{p}' for h, p in client.nodes]}")

    # Atlas M0 is a 3-node replica set, which is why transactions and change
    # streams work on the free tier. Captured for the report (Module 4).
    try:
        rs_status = client.admin.command("replSetGetStatus")
        print(f"replica set: {rs_status['set']}")
        for member in rs_status["members"]:
            print(f"  - {member['name']:40} {member['stateStr']}")
    except Exception as exc:
        print(f"replSetGetStatus unavailable: {exc}")

    db = get_db()
    print(f"database: {db.name}")
    print(f"collections: {sorted(db.list_collection_names())}")

    close_client()


if __name__ == "__main__":
    main()
