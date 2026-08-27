from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


TRACKED_RESOURCE_PATHS = (
    "/llms.txt",
    "/ai/recipe.md",
    "/banana-muffins.md",
)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def build_report(database_path: Path) -> dict[str, object]:
    connection = sqlite3.connect(str(database_path))
    connection.row_factory = sqlite3.Row

    resource_event_total = int(
        connection.execute(
            "SELECT COUNT(*) AS hit_count FROM events WHERE event_type = 'resource'"
        ).fetchone()["hit_count"]
    )
    resource_reads_total = 0
    if _table_exists(connection, "resource_reads"):
        resource_reads_total = int(
            connection.execute(
                "SELECT COUNT(*) AS hit_count FROM resource_reads"
            ).fetchone()["hit_count"]
        )

    per_path: list[dict[str, object]] = []
    for path in TRACKED_RESOURCE_PATHS:
        events_count = int(
            connection.execute(
                """
                SELECT COUNT(*) AS hit_count
                FROM events
                WHERE event_type = 'resource' AND path = ?
                """,
                (path,),
            ).fetchone()["hit_count"]
        )
        legacy_count = 0
        if _table_exists(connection, "resource_reads"):
            legacy_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS hit_count
                    FROM resource_reads
                    WHERE path = ?
                    """,
                    (path,),
                ).fetchone()["hit_count"]
            )
        per_path.append(
            {
                "path": path,
                "events_resource_count": events_count,
                "resource_reads_count": legacy_count,
            }
        )

    return {
        "database_path": str(database_path),
        "resource_reads_total": resource_reads_total,
        "events_resource_total": resource_event_total,
        "paths": per_path,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare legacy and unified resource tracking counts.")
    parser.add_argument("--database", default="events.db", help="Path to the SQLite database file.")
    args = parser.parse_args()

    report = build_report(Path(args.database))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
