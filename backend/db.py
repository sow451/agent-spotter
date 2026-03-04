from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

ALLOWED_SOURCE_KINDS = {"none", "unknown", "manual", "agent"}
ALLOWED_EVENT_TYPES = {"fetch", "hi_get", "hi_post"}
EVENTS_REFRESH_CADENCE_SECONDS = 600
TOKEN_TTL_SECONDS = 60
CRAWLER_MARKERS = (
    "bot",
    "crawl",
    "spider",
    "slurp",
    "headless",
    "gptbot",
    "claudebot",
    "bytespider",
    "facebookexternalhit",
)


class RateLimitExceeded(Exception):
    """Raised when a client exceeds the hi POST write limits."""


class SchemaCompatibilityError(RuntimeError):
    """Raised when the on-disk database schema is incompatible."""


class InvalidTokenError(Exception):
    """Raised when a token is invalid, expired, or already used."""


TokenValidationError = InvalidTokenError
InvalidOrExpiredTokenError = InvalidTokenError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utc_day(value: datetime | None = None) -> str:
    current = value or utc_now()
    return current.date().isoformat()


def _ordinal(value: int) -> str:
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def create_connection(database_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(
        database_path,
        check_same_thread=False,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database(database_path: str) -> None:
    with create_connection(database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=5000")
        _ensure_schema_compatible_or_empty(connection)
        _create_schema(connection)
        _ensure_cache_row(connection)
        rebuild_stats_cache(connection)


def extract_client_ip(request: Any, trust_proxy_headers: bool) -> str:
    if trust_proxy_headers:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            first_hop = forwarded_for.split(",")[0].strip()
            if first_hop:
                return first_hop

    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host or "unknown"


def hash_ip(ip_address: str, salt: str) -> str:
    return hashlib.sha256(f"{ip_address}{salt}".encode("utf-8")).hexdigest()


def detect_likely_crawler(user_agent: str) -> bool:
    lowered = user_agent.lower()
    return any(marker in lowered for marker in CRAWLER_MARKERS)


def build_request_context(request: Any, salt: str, trust_proxy_headers: bool) -> dict[str, Any]:
    now = utc_now()
    timestamp = utc_timestamp(now)
    ip_address = extract_client_ip(request, trust_proxy_headers)
    user_agent = request.headers.get("user-agent", "")
    return {
        "now": now,
        "ts": timestamp,
        "window_day": utc_day(now),
        "ip_hash": hash_ip(ip_address, salt),
        "user_agent": user_agent,
        "likely_crawler": detect_likely_crawler(user_agent),
    }


def record_fetch(database_path: str, context: dict[str, Any]) -> None:
    record_fetch_and_issue_token(database_path, context)


def record_fetch_and_issue_token(
    database_path: str,
    context: dict[str, Any],
) -> dict[str, Any]:
    token = _generate_token()
    token_hash = _hash_token(token)
    expires_at = utc_timestamp(context["now"] + timedelta(seconds=TOKEN_TTL_SECONDS))

    with create_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_cache_window_current(connection, context["window_day"])
            cursor = connection.execute(
                """
                INSERT INTO events (
                    ts,
                    event_type,
                    path,
                    agent_name,
                    message,
                    source_kind,
                    user_agent,
                    ip_hash,
                    likely_crawler,
                    token_used
                ) VALUES (?, 'fetch', '/agent.txt', NULL, NULL, 'none', ?, ?, ?, 0)
                """,
                (
                    context["ts"],
                    context["user_agent"],
                    context["ip_hash"],
                    int(context["likely_crawler"]),
                ),
            )
            fetch_event_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO hi_tokens (
                    token_hash,
                    issued_at,
                    expires_at,
                    used_at,
                    fetch_event_id,
                    issued_ip_hash
                ) VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (
                    token_hash,
                    context["ts"],
                    expires_at,
                    fetch_event_id,
                    context["ip_hash"],
                ),
            )
            _touch_source_window(
                connection=connection,
                window_day=context["window_day"],
                event_type="fetch",
                source_kind="none",
                token_used=0,
                ip_hash=context["ip_hash"],
                ts=context["ts"],
            )
            rebuild_stats_cache(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    return {"token": token}


def record_hi_get(
    database_path: str,
    context: dict[str, Any],
    agent_name: str,
    message: str,
    source_kind: str,
) -> dict[str, Any]:
    return _record_hi_event(
        database_path=database_path,
        context=context,
        agent_name=agent_name,
        message=message,
        source_kind=source_kind,
        token=None,
        signal="hi_get",
    )


def record_hi_post(
    database_path: str,
    context: dict[str, Any],
    agent_name: str,
    message: str,
    source_kind: str,
    token: str | None = None,
    signal: str | None = None,
) -> dict[str, Any]:
    requested_signal = signal or ("hi_post_token" if token else "hi_post")
    return _record_hi_event(
        database_path=database_path,
        context=context,
        agent_name=agent_name,
        message=message,
        source_kind=source_kind,
        token=token,
        signal=requested_signal,
    )


def list_events(
    database_path: str,
    *,
    event_type: str,
    source: str,
    hide_likely_crawlers: bool,
    q: str,
    limit: int,
    before_id: int | None,
) -> dict[str, Any]:
    with create_connection(database_path) as connection:
        now = utc_now()
        _ensure_cache_window_current(connection, utc_day(now))
        stats = _read_stats_row(connection)

        conditions: list[str] = []
        params: list[Any] = []

        if event_type == "fetch":
            conditions.append("event_type = 'fetch'")
        elif event_type == "hi":
            conditions.append("event_type IN ('hi_get', 'hi_post')")
            if source != "all":
                conditions.append("source_kind = ?")
                params.append(source)
        elif source != "all":
            conditions.append(
                "(event_type = 'fetch' OR (event_type IN ('hi_get', 'hi_post') AND source_kind = ?))"
            )
            params.append(source)

        if hide_likely_crawlers:
            conditions.append("likely_crawler = 0")

        if before_id is not None:
            conditions.append("id < ?")
            params.append(before_id)

        if q:
            conditions.append(
                """
                (
                    LOWER(COALESCE(agent_name, '')) LIKE ?
                    OR LOWER(COALESCE(message, '')) LIKE ?
                    OR LOWER(COALESCE(user_agent, '')) LIKE ?
                )
                """
            )
            search_term = f"%{q.lower()}%"
            params.extend([search_term, search_term, search_term])

        where_clause = ""
        if conditions:
            where_clause = "WHERE " + " AND ".join(conditions)

        rows = connection.execute(
            f"""
            SELECT
                id,
                ts,
                event_type,
                path,
                agent_name,
                message,
                source_kind,
                user_agent,
                likely_crawler,
                token_used
            FROM events
            {where_clause}
            ORDER BY id DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()

    refresh = _refresh_payload(now)
    counters = {
        "fetch": int(stats["fetch_count"]),
        "hi_get": int(stats["hi_get_count"]),
        "hi_post": int(stats["hi_post_count"]),
        "hi_post_token": int(stats["hi_post_token_count"]),
        "hi_total": int(stats["hi_total_count"]),
        "hi_unknown": int(stats["hi_unknown_count"]),
        "hi_manual": int(stats["hi_manual_count"]),
        "hi_agent": _derived_hi_agent_count(stats),
        "fetch_unique_utc_day": int(stats["fetch_unique_utc_day"]),
        "hi_total_unique_utc_day": int(stats["hi_total_unique_utc_day"]),
        "hi_post_token_unique_utc_day": int(stats["hi_post_token_unique_utc_day"]),
        "ratio_total": _fetch_per_hi_ratio(stats["fetch_count"], stats["hi_total_count"]),
        "ratio_unknown": _ratio(stats["hi_unknown_count"], stats["fetch_count"]),
    }
    events = [
        {
            "id": int(row["id"]),
            "ts": row["ts"],
            "event_type": row["event_type"],
            "path": row["path"],
            "agent_name": row["agent_name"],
            "message": row["message"],
            "source_kind": row["source_kind"],
            "user_agent": row["user_agent"],
            "likely_crawler": bool(row["likely_crawler"]),
            "token_used": bool(row["token_used"]),
        }
        for row in rows
    ]
    has_more = len(events) == limit

    return {
        "refresh": refresh,
        "counters": counters,
        "events": events,
        "has_more": has_more,
    }


def rebuild_stats_cache(connection: sqlite3.Connection) -> None:
    now = utc_now()
    current_day = utc_day(now)
    updated_at = utc_timestamp(now)

    _rebuild_source_windows_for_day(connection, current_day)

    stats = {
        "fetch_count": _count_events(connection, "event_type = 'fetch'"),
        "hi_get_count": _count_events(connection, "event_type = 'hi_get'"),
        "hi_post_count": _count_events(
            connection,
            "event_type = 'hi_post' AND token_used = 0",
        ),
        "hi_post_token_count": _count_events(
            connection,
            "event_type = 'hi_post' AND token_used = 1",
        ),
        "hi_unknown_count": _count_events(
            connection,
            "event_type IN ('hi_get', 'hi_post') AND source_kind = 'unknown'",
        ),
        "hi_manual_count": _count_events(
            connection,
            "event_type IN ('hi_get', 'hi_post') AND source_kind = 'manual'",
        ),
        "fetch_unique_utc_day": _count_distinct_ip_for_day(
            connection,
            current_day,
            "event_type = 'fetch'",
        ),
        "hi_total_unique_utc_day": _count_distinct_ip_for_day(
            connection,
            current_day,
            "event_type IN ('hi_get', 'hi_post')",
        ),
        "hi_post_token_unique_utc_day": _count_distinct_ip_for_day(
            connection,
            current_day,
            "event_type = 'hi_post' AND token_used = 1",
        ),
    }
    stats["hi_total_count"] = (
        stats["hi_get_count"] + stats["hi_post_count"] + stats["hi_post_token_count"]
    )

    connection.execute(
        """
        INSERT INTO stats_cache (
            cache_key,
            cache_window_day,
            fetch_count,
            hi_get_count,
            hi_post_count,
            hi_post_token_count,
            hi_total_count,
            hi_unknown_count,
            hi_manual_count,
            fetch_unique_utc_day,
            hi_total_unique_utc_day,
            hi_post_token_unique_utc_day,
            updated_at
        ) VALUES (
            'global', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        ON CONFLICT(cache_key) DO UPDATE SET
            cache_window_day = excluded.cache_window_day,
            fetch_count = excluded.fetch_count,
            hi_get_count = excluded.hi_get_count,
            hi_post_count = excluded.hi_post_count,
            hi_post_token_count = excluded.hi_post_token_count,
            hi_total_count = excluded.hi_total_count,
            hi_unknown_count = excluded.hi_unknown_count,
            hi_manual_count = excluded.hi_manual_count,
            fetch_unique_utc_day = excluded.fetch_unique_utc_day,
            hi_total_unique_utc_day = excluded.hi_total_unique_utc_day,
            hi_post_token_unique_utc_day = excluded.hi_post_token_unique_utc_day,
            updated_at = excluded.updated_at
        """,
        (
            current_day,
            stats["fetch_count"],
            stats["hi_get_count"],
            stats["hi_post_count"],
            stats["hi_post_token_count"],
            stats["hi_total_count"],
            stats["hi_unknown_count"],
            stats["hi_manual_count"],
            stats["fetch_unique_utc_day"],
            stats["hi_total_unique_utc_day"],
            stats["hi_post_token_unique_utc_day"],
            updated_at,
        ),
    )


def _record_hi_event(
    *,
    database_path: str,
    context: dict[str, Any],
    agent_name: str,
    message: str,
    source_kind: str,
    token: str | None,
    signal: str,
) -> dict[str, Any]:
    event_type = "hi_get" if signal == "hi_get" else "hi_post"
    token_used = 1 if token else 0

    with create_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_cache_window_current(connection, context["window_day"])

            if event_type == "hi_post" and _is_rate_limited(connection, context["ip_hash"], context["now"]):
                connection.rollback()
                raise RateLimitExceeded()

            if token:
                _validate_and_consume_token(
                    connection=connection,
                    token=token,
                    current_time=context["now"],
                    used_at=context["ts"],
                )

            cursor = connection.execute(
                """
                INSERT INTO events (
                    ts,
                    event_type,
                    path,
                    agent_name,
                    message,
                    source_kind,
                    user_agent,
                    ip_hash,
                    likely_crawler,
                    token_used
                ) VALUES (?, ?, '/hi', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context["ts"],
                    event_type,
                    agent_name,
                    message,
                    source_kind,
                    context["user_agent"],
                    context["ip_hash"],
                    int(context["likely_crawler"]),
                    token_used,
                ),
            )

            _touch_source_window(
                connection=connection,
                window_day=context["window_day"],
                event_type=event_type,
                source_kind=source_kind,
                token_used=token_used,
                ip_hash=context["ip_hash"],
                ts=context["ts"],
            )
            rebuild_stats_cache(connection)
            stats = _read_stats_row(connection)
            connection.commit()
        except InvalidTokenError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise

    signal_name = "hi_post_token" if token_used else signal
    token_status = "valid" if token_used else ("missing" if event_type == "hi_post" else None)
    signal_count = (
        int(stats["hi_post_token_count"])
        if signal_name == "hi_post_token"
        else int(stats["hi_post_count"])
        if signal_name == "hi_post"
        else int(stats["hi_get_count"])
    )
    caller_place = _ordinal(signal_count)
    reward_message = (
        f"You said hi via POST with a valid token. You are the {caller_place} caller on this path."
        if signal_name == "hi_post_token"
        else f"You said hi via POST. You are the {caller_place} caller on this path."
        if signal_name == "hi_post"
        else f"You said hi via the easy path. You are the {caller_place} caller on this path."
    )

    payload = {
        "status": "ok",
        "event_id": int(cursor.lastrowid),
        "signal": signal_name,
        "hi_total": int(stats["hi_total_count"]),
        "hi_get": int(stats["hi_get_count"]),
        "hi_post": int(stats["hi_post_count"]),
        "hi_post_token": int(stats["hi_post_token_count"]),
        "ratio_total": _fetch_per_hi_ratio(stats["fetch_count"], stats["hi_total_count"]),
        "ratio_unknown": _ratio(stats["hi_unknown_count"], stats["fetch_count"]),
        "reward_message": reward_message,
    }
    if token_status is not None:
        payload["token_status"] = token_status
    return payload


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('fetch', 'hi_get', 'hi_post')),
            path TEXT NOT NULL,
            agent_name TEXT,
            message TEXT,
            source_kind TEXT NOT NULL CHECK (source_kind IN ('none', 'unknown', 'manual', 'agent')),
            user_agent TEXT NOT NULL DEFAULT '',
            ip_hash TEXT NOT NULL,
            likely_crawler INTEGER NOT NULL CHECK (likely_crawler IN (0, 1)),
            token_used INTEGER NOT NULL CHECK (token_used IN (0, 1))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS hi_tokens (
            token_hash TEXT PRIMARY KEY,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            fetch_event_id INTEGER NOT NULL,
            issued_ip_hash TEXT NOT NULL,
            FOREIGN KEY(fetch_event_id) REFERENCES events(id)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS source_windows (
            window_day TEXT NOT NULL,
            event_type TEXT NOT NULL,
            source_kind TEXT NOT NULL,
            token_used INTEGER NOT NULL CHECK (token_used IN (0, 1)),
            ip_hash TEXT NOT NULL,
            event_count INTEGER NOT NULL DEFAULT 1,
            first_ts TEXT NOT NULL,
            last_ts TEXT NOT NULL,
            PRIMARY KEY (window_day, event_type, source_kind, token_used, ip_hash)
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS stats_cache (
            cache_key TEXT PRIMARY KEY,
            cache_window_day TEXT NOT NULL,
            fetch_count INTEGER NOT NULL DEFAULT 0,
            hi_get_count INTEGER NOT NULL DEFAULT 0,
            hi_post_count INTEGER NOT NULL DEFAULT 0,
            hi_post_token_count INTEGER NOT NULL DEFAULT 0,
            hi_total_count INTEGER NOT NULL DEFAULT 0,
            hi_unknown_count INTEGER NOT NULL DEFAULT 0,
            hi_manual_count INTEGER NOT NULL DEFAULT 0,
            fetch_unique_utc_day INTEGER NOT NULL DEFAULT 0,
            hi_total_unique_utc_day INTEGER NOT NULL DEFAULT 0,
            hi_post_token_unique_utc_day INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """
    )

    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_desc ON events(ts DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_events_event_id ON events(event_type, id DESC)")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_ip_event_ts ON events(ip_hash, event_type, ts DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_source_id ON events(source_kind, id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_events_crawler_id ON events(likely_crawler, id DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_hi_tokens_expires ON hi_tokens(expires_at)"
    )


def _ensure_schema_compatible_or_empty(connection: sqlite3.Connection) -> None:
    existing_rows = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    existing_tables = {row["name"] for row in existing_rows}
    if not existing_tables:
        return

    required_columns = {
        "events": {
            "ts",
            "event_type",
            "path",
            "agent_name",
            "message",
            "source_kind",
            "user_agent",
            "ip_hash",
            "likely_crawler",
            "token_used",
        },
        "source_windows": {
            "window_day",
            "event_type",
            "source_kind",
            "token_used",
            "ip_hash",
            "event_count",
            "first_ts",
            "last_ts",
        },
        "stats_cache": {
            "cache_key",
            "cache_window_day",
            "fetch_count",
            "hi_get_count",
            "hi_post_count",
            "hi_post_token_count",
            "hi_total_count",
            "hi_unknown_count",
            "hi_manual_count",
            "fetch_unique_utc_day",
            "hi_total_unique_utc_day",
            "hi_post_token_unique_utc_day",
            "updated_at",
        },
        "hi_tokens": {
            "token_hash",
            "issued_at",
            "expires_at",
            "used_at",
            "fetch_event_id",
            "issued_ip_hash",
        },
    }

    for table_name, required in required_columns.items():
        if table_name not in existing_tables:
            continue
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        available = {row["name"] for row in rows}
        if not required.issubset(available):
            raise SchemaCompatibilityError("incompatible database schema detected")


def _ensure_cache_row(connection: sqlite3.Connection) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT OR IGNORE INTO stats_cache (
            cache_key,
            cache_window_day,
            fetch_count,
            hi_get_count,
            hi_post_count,
            hi_post_token_count,
            hi_total_count,
            hi_unknown_count,
            hi_manual_count,
            fetch_unique_utc_day,
            hi_total_unique_utc_day,
            hi_post_token_unique_utc_day,
            updated_at
        ) VALUES ('global', ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, ?)
        """,
        (utc_day(now), utc_timestamp(now)),
    )


def _ensure_cache_window_current(connection: sqlite3.Connection, window_day: str) -> None:
    row = _read_stats_row(connection)
    if row["cache_window_day"] != window_day:
        rebuild_stats_cache(connection)


def _read_stats_row(connection: sqlite3.Connection) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM stats_cache WHERE cache_key = 'global'"
    ).fetchone()
    if row is None:
        _ensure_cache_row(connection)
        rebuild_stats_cache(connection)
        row = connection.execute(
            "SELECT * FROM stats_cache WHERE cache_key = 'global'"
        ).fetchone()
    if row is None:
        raise SchemaCompatibilityError("missing global stats cache row")
    return row


def _touch_source_window(
    *,
    connection: sqlite3.Connection,
    window_day: str,
    event_type: str,
    source_kind: str,
    token_used: int,
    ip_hash: str,
    ts: str,
) -> None:
    connection.execute(
        """
        INSERT INTO source_windows (
            window_day,
            event_type,
            source_kind,
            token_used,
            ip_hash,
            event_count,
            first_ts,
            last_ts
        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(window_day, event_type, source_kind, token_used, ip_hash)
        DO UPDATE SET
            event_count = event_count + 1,
            last_ts = excluded.last_ts
        """,
        (
            window_day,
            event_type,
            source_kind,
            token_used,
            ip_hash,
            ts,
            ts,
        ),
    )


def _is_rate_limited(connection: sqlite3.Connection, ip_hash: str, now: datetime) -> bool:
    minute_cutoff = utc_timestamp(now - timedelta(seconds=60))
    hour_cutoff = utc_timestamp(now - timedelta(seconds=3600))

    minute_count = connection.execute(
        """
        SELECT COUNT(*) AS hit_count
        FROM events
        WHERE event_type = 'hi_post' AND ip_hash = ? AND ts >= ?
        """,
        (ip_hash, minute_cutoff),
    ).fetchone()["hit_count"]
    if minute_count >= 3:
        return True

    hour_count = connection.execute(
        """
        SELECT COUNT(*) AS hit_count
        FROM events
        WHERE event_type = 'hi_post' AND ip_hash = ? AND ts >= ?
        """,
        (ip_hash, hour_cutoff),
    ).fetchone()["hit_count"]
    return hour_count >= 20


def _validate_and_consume_token(
    *,
    connection: sqlite3.Connection,
    token: str,
    current_time: datetime,
    used_at: str,
) -> None:
    token_hash = _hash_token(token)
    row = connection.execute(
        """
        SELECT token_hash, expires_at, used_at
        FROM hi_tokens
        WHERE token_hash = ?
        """,
        (token_hash,),
    ).fetchone()

    now_ts = utc_timestamp(current_time)
    if row is None or row["used_at"] is not None or now_ts >= row["expires_at"]:
        raise InvalidTokenError(
            "Token invalid or expired. Fetch /agent.txt again for a fresh token."
        )

    connection.execute(
        """
        UPDATE hi_tokens
        SET used_at = ?
        WHERE token_hash = ? AND used_at IS NULL
        """,
        (used_at, token_hash),
    )


def _rebuild_source_windows_for_day(connection: sqlite3.Connection, window_day: str) -> None:
    day_prefix = f"{window_day}%"
    connection.execute("DELETE FROM source_windows WHERE window_day = ?", (window_day,))
    rows = connection.execute(
        """
        SELECT
            ? AS window_day,
            event_type,
            source_kind,
            token_used,
            ip_hash,
            COUNT(*) AS event_count,
            MIN(ts) AS first_ts,
            MAX(ts) AS last_ts
        FROM events
        WHERE ts LIKE ?
        GROUP BY event_type, source_kind, token_used, ip_hash
        """,
        (window_day, day_prefix),
    ).fetchall()
    for row in rows:
        connection.execute(
            """
            INSERT INTO source_windows (
                window_day,
                event_type,
                source_kind,
                token_used,
                ip_hash,
                event_count,
                first_ts,
                last_ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["window_day"],
                row["event_type"],
                row["source_kind"],
                row["token_used"],
                row["ip_hash"],
                row["event_count"],
                row["first_ts"],
                row["last_ts"],
            ),
        )


def _count_events(connection: sqlite3.Connection, where_clause: str) -> int:
    query = f"SELECT COUNT(*) AS hit_count FROM events WHERE {where_clause}"
    return int(connection.execute(query).fetchone()["hit_count"])


def _count_distinct_ip_for_day(
    connection: sqlite3.Connection,
    window_day: str,
    where_clause: str,
) -> int:
    day_prefix = f"{window_day}%"
    query = f"""
        SELECT COUNT(DISTINCT ip_hash) AS unique_count
        FROM events
        WHERE ts LIKE ? AND {where_clause}
    """
    return int(connection.execute(query, (day_prefix,)).fetchone()["unique_count"])


def _derived_hi_agent_count(stats: sqlite3.Row) -> int:
    derived = (
        int(stats["hi_total_count"])
        - int(stats["hi_unknown_count"])
        - int(stats["hi_manual_count"])
    )
    return max(0, derived)


def _ratio(numerator: int, denominator: int) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 4)


def _fetch_per_hi_ratio(fetch_count: int, hi_total_count: int) -> float:
    if not hi_total_count:
        return 0.0
    return round(float(fetch_count) / float(hi_total_count), 4)


def _refresh_payload(now: datetime) -> dict[str, Any]:
    current_epoch = int(now.timestamp())
    next_epoch = ((current_epoch // EVENTS_REFRESH_CADENCE_SECONDS) + 1) * EVENTS_REFRESH_CADENCE_SECONDS
    next_time = datetime.fromtimestamp(next_epoch, tz=timezone.utc)
    return {
        "cadence_seconds": EVENTS_REFRESH_CADENCE_SECONDS,
        "cadence_minutes": EVENTS_REFRESH_CADENCE_SECONDS // 60,
        "last_refreshed_at": utc_timestamp(now),
        "next_refresh_at": utc_timestamp(next_time),
    }


def _generate_token() -> str:
    return secrets.token_urlsafe(9)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

