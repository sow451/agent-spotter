from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

ALLOWED_SOURCE_KINDS = {"none", "unknown", "manual", "agent"}
ALLOWED_EVENT_TYPES = {"fetch", "hi_get", "hi_post", "resource"}
EVENTS_REFRESH_CADENCE_SECONDS = 600
TOKEN_TTL_SECONDS = 60
FETCH_RATE_LIMIT_PER_MINUTE = 60
FETCH_RATE_LIMIT_PER_HOUR = 600
HI_GET_RATE_LIMIT_PER_MINUTE = 20
HI_GET_RATE_LIMIT_PER_HOUR = 240
HI_POST_RATE_LIMIT_PER_MINUTE = 3
HI_POST_RATE_LIMIT_PER_HOUR = 20
EVENTS_RATE_LIMIT_PER_MINUTE = 1500
EVENTS_RATE_LIMIT_PER_HOUR = 20000
EVENTS_PUBLIC_RATE_LIMIT_PER_MINUTE = 300
EVENTS_PUBLIC_RATE_LIMIT_PER_HOUR = 4000
REQUIRED_INDEX_NAMES = {
    "idx_events_ts_desc",
    "idx_events_event_id",
    "idx_events_ip_event_ts",
    "idx_events_source_id",
    "idx_events_crawler_id",
    "idx_hi_tokens_expires",
}
REQUIRED_INDEX_SQL_FRAGMENTS = {
    "idx_events_ts_desc": "on events(ts desc)",
    "idx_events_event_id": "on events(event_type, id desc)",
    "idx_events_ip_event_ts": "on events(ip_hash, event_type, ts desc)",
    "idx_events_source_id": "on events(source_kind, id desc)",
    "idx_events_crawler_id": "on events(likely_crawler, id desc)",
    "idx_hi_tokens_expires": "on hi_tokens(expires_at)",
}
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
    """Raised when a client exceeds public write limits."""


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
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        _ensure_schema_compatible_or_empty(connection)
        _create_schema(connection)
        _cleanup_expired_tokens(connection, utc_now())
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


def enforce_events_rate_limit(
    database_path: str,
    context: dict[str, Any],
    *,
    minute_limit: int | None = None,
    hour_limit: int | None = None,
) -> None:
    resolved_minute_limit = EVENTS_RATE_LIMIT_PER_MINUTE if minute_limit is None else minute_limit
    resolved_hour_limit = EVENTS_RATE_LIMIT_PER_HOUR if hour_limit is None else hour_limit

    _enforce_endpoint_rate_limit(
        database_path=database_path,
        context=context,
        endpoint="/events",
        minute_limit=resolved_minute_limit,
        hour_limit=resolved_hour_limit,
    )


def enforce_events_public_rate_limit(
    database_path: str,
    context: dict[str, Any],
    *,
    minute_limit: int | None = None,
    hour_limit: int | None = None,
) -> None:
    resolved_minute_limit = (
        EVENTS_PUBLIC_RATE_LIMIT_PER_MINUTE if minute_limit is None else minute_limit
    )
    resolved_hour_limit = EVENTS_PUBLIC_RATE_LIMIT_PER_HOUR if hour_limit is None else hour_limit

    _enforce_endpoint_rate_limit(
        database_path=database_path,
        context=context,
        endpoint="/events/public",
        minute_limit=resolved_minute_limit,
        hour_limit=resolved_hour_limit,
    )


def _enforce_endpoint_rate_limit(
    *,
    database_path: str,
    context: dict[str, Any],
    endpoint: str,
    minute_limit: int,
    hour_limit: int,
) -> None:

    with create_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            _cleanup_endpoint_hits(connection, context["now"])

            if _is_endpoint_rate_limited(
                connection=connection,
                endpoint=endpoint,
                ip_hash=context["ip_hash"],
                now=context["now"],
                minute_limit=minute_limit,
                hour_limit=hour_limit,
            ):
                connection.rollback()
                raise RateLimitExceeded()

            connection.execute(
                """
                INSERT INTO endpoint_hits (ts, endpoint, ip_hash)
                VALUES (?, ?, ?)
                """,
                (context["ts"], endpoint, context["ip_hash"]),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


def record_fetch(database_path: str, context: dict[str, Any]) -> None:
    record_fetch_and_issue_token(database_path, context)


def record_resource_access(
    database_path: str,
    context: dict[str, Any],
    *,
    path: str,
) -> None:
    with create_connection(database_path) as connection:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
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
                ) VALUES (?, 'resource', ?, NULL, NULL, 'none', ?, ?, ?, 0)
                """,
                (
                    context["ts"],
                    path,
                    context["user_agent"],
                    context["ip_hash"],
                    int(context["likely_crawler"]),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if _is_legacy_event_type_check_error(exc):
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO resource_reads (
                        ts,
                        path,
                        user_agent,
                        ip_hash,
                        likely_crawler
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        context["ts"],
                        path,
                        context["user_agent"],
                        context["ip_hash"],
                        int(context["likely_crawler"]),
                    ),
                )
                connection.commit()
                return
            raise
        except Exception:
            connection.rollback()
            raise


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
            _cleanup_expired_tokens(connection, context["now"])

            if _is_rate_limited(
                connection=connection,
                ip_hash=context["ip_hash"],
                now=context["now"],
                event_type="fetch",
                minute_limit=FETCH_RATE_LIMIT_PER_MINUTE,
                hour_limit=FETCH_RATE_LIMIT_PER_HOUR,
            ):
                connection.rollback()
                raise RateLimitExceeded()

            is_new_fetch_unique_utc_day = not _source_window_exists(
                connection=connection,
                window_day=context["window_day"],
                event_type="fetch",
                ip_hash=context["ip_hash"],
                token_used=0,
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
            _apply_event_to_stats_cache(
                connection=connection,
                event_type="fetch",
                source_kind="none",
                token_used=0,
                ts=context["ts"],
                is_new_fetch_unique_utc_day=is_new_fetch_unique_utc_day,
                is_new_hi_unique_utc_day=False,
                is_new_hi_post_token_unique_utc_day=False,
            )
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
        resource_count = _count_events(connection, "event_type = 'resource'")

    refresh = _refresh_payload(now)
    counters = {
        "resource": int(resource_count),
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
            _cleanup_expired_tokens(connection, context["now"])

            minute_limit = (
                HI_POST_RATE_LIMIT_PER_MINUTE
                if event_type == "hi_post"
                else HI_GET_RATE_LIMIT_PER_MINUTE
            )
            hour_limit = (
                HI_POST_RATE_LIMIT_PER_HOUR
                if event_type == "hi_post"
                else HI_GET_RATE_LIMIT_PER_HOUR
            )
            if _is_rate_limited(
                connection=connection,
                ip_hash=context["ip_hash"],
                now=context["now"],
                event_type=event_type,
                minute_limit=minute_limit,
                hour_limit=hour_limit,
            ):
                connection.rollback()
                raise RateLimitExceeded()

            if token:
                _validate_and_consume_token(
                    connection=connection,
                    token=token,
                    current_time=context["now"],
                    used_at=context["ts"],
                )

            is_new_hi_unique_utc_day = not _has_hi_source_for_day(
                connection=connection,
                window_day=context["window_day"],
                ip_hash=context["ip_hash"],
            )
            is_new_hi_post_token_unique_utc_day = (
                token_used == 1
                and not _has_hi_post_token_source_for_day(
                    connection=connection,
                    window_day=context["window_day"],
                    ip_hash=context["ip_hash"],
                )
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
            _apply_event_to_stats_cache(
                connection=connection,
                event_type=event_type,
                source_kind=source_kind,
                token_used=token_used,
                ts=context["ts"],
                is_new_fetch_unique_utc_day=False,
                is_new_hi_unique_utc_day=is_new_hi_unique_utc_day,
                is_new_hi_post_token_unique_utc_day=is_new_hi_post_token_unique_utc_day,
            )
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
            event_type TEXT NOT NULL CHECK (event_type IN ('fetch', 'hi_get', 'hi_post', 'resource')),
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
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS endpoint_hits (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            ip_hash TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS resource_reads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            path TEXT NOT NULL,
            user_agent TEXT NOT NULL DEFAULT '',
            ip_hash TEXT NOT NULL,
            likely_crawler INTEGER NOT NULL CHECK (likely_crawler IN (0, 1))
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
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_endpoint_hits_endpoint_ip_ts ON endpoint_hits(endpoint, ip_hash, ts DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_source_windows_day_ip_event_token ON source_windows(window_day, ip_hash, event_type, token_used)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_resource_reads_path_id ON resource_reads(path, id DESC)"
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
            raise _schema_error(f"{table_name} missing required columns")

    _validate_table_primary_key(connection, "events", ["id"])
    _validate_table_primary_key(connection, "hi_tokens", ["token_hash"])
    _validate_table_primary_key(
        connection,
        "source_windows",
        ["window_day", "event_type", "source_kind", "token_used", "ip_hash"],
    )
    _validate_table_primary_key(connection, "stats_cache", ["cache_key"])

    _validate_not_null_columns(
        connection,
        "events",
        {
            "ts",
            "event_type",
            "path",
            "source_kind",
            "user_agent",
            "ip_hash",
            "likely_crawler",
            "token_used",
        },
    )
    _validate_not_null_columns(
        connection,
        "hi_tokens",
        {"token_hash", "issued_at", "expires_at", "fetch_event_id", "issued_ip_hash"},
    )
    _validate_not_null_columns(
        connection,
        "source_windows",
        {
            "window_day",
            "event_type",
            "source_kind",
            "token_used",
            "ip_hash",
            "event_count",
            "first_ts",
            "last_ts",
        },
    )
    _validate_not_null_columns(
        connection,
        "stats_cache",
        {
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
    )

    _validate_events_event_type_check(connection)
    _validate_check_fragment(
        connection,
        "events",
        "check (source_kind in ('none', 'unknown', 'manual', 'agent'))",
    )
    _validate_check_fragment(
        connection,
        "events",
        "check (likely_crawler in (0, 1))",
    )
    _validate_check_fragment(
        connection,
        "events",
        "check (token_used in (0, 1))",
    )
    _validate_check_fragment(
        connection,
        "source_windows",
        "check (token_used in (0, 1))",
    )

    _validate_hi_tokens_foreign_key(connection)
    _validate_required_indexes(connection)


def _schema_error(reason: str) -> SchemaCompatibilityError:
    return SchemaCompatibilityError(f"incompatible database schema detected: {reason}")


def _validate_table_primary_key(
    connection: sqlite3.Connection,
    table_name: str,
    expected_columns: list[str],
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not rows:
        return

    pk_rows = [row for row in rows if int(row["pk"]) > 0]
    ordered_pk_columns = [
        row["name"] for row in sorted(pk_rows, key=lambda item: int(item["pk"]))
    ]
    if ordered_pk_columns != expected_columns:
        raise _schema_error(f"{table_name} primary key mismatch")


def _validate_not_null_columns(
    connection: sqlite3.Connection,
    table_name: str,
    required_not_null_columns: set[str],
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not rows:
        return

    nullable_required = [
        row["name"]
        for row in rows
        if row["name"] in required_not_null_columns
        and int(row["notnull"]) != 1
        and int(row["pk"]) == 0
    ]
    if nullable_required:
        raise _schema_error(
            f"{table_name} allows NULL for required columns: {', '.join(sorted(nullable_required))}"
        )


def _normalized_create_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    if row is None or not row["sql"]:
        return ""
    return _normalize_sql(str(row["sql"]))


def _normalize_sql(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    for ch in ('"', "`", "[", "]"):
        normalized = normalized.replace(ch, "")
    return normalized


def _validate_check_fragment(connection: sqlite3.Connection, table_name: str, fragment: str) -> None:
    normalized_sql = _normalized_create_sql(connection, table_name)
    if not normalized_sql:
        return
    normalized_fragment = " ".join(fragment.strip().lower().split())
    if normalized_fragment not in normalized_sql:
        raise _schema_error(f"{table_name} missing required CHECK constraint")


def _validate_events_event_type_check(connection: sqlite3.Connection) -> None:
    normalized_sql = _normalized_create_sql(connection, "events")
    if not normalized_sql:
        return

    allowed_fragments = (
        "check (event_type in ('fetch', 'hi_get', 'hi_post', 'resource'))",
        "check (event_type in ('fetch', 'hi_get', 'hi_post'))",
    )
    normalized_allowed = {
        " ".join(fragment.strip().lower().split()) for fragment in allowed_fragments
    }
    if not any(fragment in normalized_sql for fragment in normalized_allowed):
        raise _schema_error("events missing required event_type CHECK constraint")


def _is_legacy_event_type_check_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc).lower()
    return "check constraint failed" in message and "event_type" in message


def _validate_hi_tokens_foreign_key(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA foreign_key_list(hi_tokens)").fetchall()
    if not rows:
        raise _schema_error("hi_tokens missing required foreign key")

    has_required_fk = any(
        row["table"] == "events" and row["from"] == "fetch_event_id" and row["to"] == "id"
        for row in rows
    )
    if not has_required_fk:
        raise _schema_error("hi_tokens foreign key mismatch")


def _validate_required_indexes(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT name, tbl_name, sql
        FROM sqlite_master
        WHERE type = 'index'
          AND name NOT LIKE 'sqlite_%'
        """
    ).fetchall()
    by_name = {str(row["name"]): row for row in rows}
    existing_names = set(by_name.keys())
    missing = sorted(REQUIRED_INDEX_NAMES - existing_names)
    if missing:
        raise _schema_error(f"missing required indexes: {', '.join(missing)}")

    mismatched: list[str] = []
    for index_name, expected_fragment in REQUIRED_INDEX_SQL_FRAGMENTS.items():
        row = by_name.get(index_name)
        if row is None:
            continue
        sql_text = row["sql"]
        if not sql_text:
            mismatched.append(index_name)
            continue
        if _normalize_sql(expected_fragment) not in _normalize_sql(str(sql_text)):
            mismatched.append(index_name)
    if mismatched:
        raise _schema_error(
            f"required index definitions mismatch: {', '.join(sorted(mismatched))}"
        )


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


def _source_window_exists(
    *,
    connection: sqlite3.Connection,
    window_day: str,
    event_type: str,
    ip_hash: str,
    token_used: int | None = None,
) -> bool:
    conditions = ["window_day = ?", "event_type = ?", "ip_hash = ?"]
    params: list[Any] = [window_day, event_type, ip_hash]
    if token_used is not None:
        conditions.append("token_used = ?")
        params.append(token_used)

    where_clause = " AND ".join(conditions)
    row = connection.execute(
        f"""
        SELECT 1
        FROM source_windows
        WHERE {where_clause}
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return row is not None


def _has_hi_source_for_day(
    *,
    connection: sqlite3.Connection,
    window_day: str,
    ip_hash: str,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM source_windows
        WHERE window_day = ?
          AND ip_hash = ?
          AND event_type IN ('hi_get', 'hi_post')
        LIMIT 1
        """,
        (window_day, ip_hash),
    ).fetchone()
    return row is not None


def _has_hi_post_token_source_for_day(
    *,
    connection: sqlite3.Connection,
    window_day: str,
    ip_hash: str,
) -> bool:
    return _source_window_exists(
        connection=connection,
        window_day=window_day,
        event_type="hi_post",
        ip_hash=ip_hash,
        token_used=1,
    )


def _apply_event_to_stats_cache(
    *,
    connection: sqlite3.Connection,
    event_type: str,
    source_kind: str,
    token_used: int,
    ts: str,
    is_new_fetch_unique_utc_day: bool,
    is_new_hi_unique_utc_day: bool,
    is_new_hi_post_token_unique_utc_day: bool,
) -> None:
    _read_stats_row(connection)

    is_hi_event = event_type in {"hi_get", "hi_post"}
    fetch_delta = 1 if event_type == "fetch" else 0
    hi_get_delta = 1 if event_type == "hi_get" else 0
    hi_post_delta = 1 if event_type == "hi_post" and token_used == 0 else 0
    hi_post_token_delta = 1 if event_type == "hi_post" and token_used == 1 else 0
    hi_total_delta = 1 if is_hi_event else 0
    hi_unknown_delta = 1 if is_hi_event and source_kind == "unknown" else 0
    hi_manual_delta = 1 if is_hi_event and source_kind == "manual" else 0
    fetch_unique_delta = 1 if is_new_fetch_unique_utc_day else 0
    hi_total_unique_delta = 1 if is_new_hi_unique_utc_day else 0
    hi_post_token_unique_delta = 1 if is_new_hi_post_token_unique_utc_day else 0

    connection.execute(
        """
        UPDATE stats_cache
        SET
            fetch_count = fetch_count + ?,
            hi_get_count = hi_get_count + ?,
            hi_post_count = hi_post_count + ?,
            hi_post_token_count = hi_post_token_count + ?,
            hi_total_count = hi_total_count + ?,
            hi_unknown_count = hi_unknown_count + ?,
            hi_manual_count = hi_manual_count + ?,
            fetch_unique_utc_day = fetch_unique_utc_day + ?,
            hi_total_unique_utc_day = hi_total_unique_utc_day + ?,
            hi_post_token_unique_utc_day = hi_post_token_unique_utc_day + ?,
            updated_at = ?
        WHERE cache_key = 'global'
        """,
        (
            fetch_delta,
            hi_get_delta,
            hi_post_delta,
            hi_post_token_delta,
            hi_total_delta,
            hi_unknown_delta,
            hi_manual_delta,
            fetch_unique_delta,
            hi_total_unique_delta,
            hi_post_token_unique_delta,
            ts,
        ),
    )


def _is_rate_limited(
    *,
    connection: sqlite3.Connection,
    ip_hash: str,
    now: datetime,
    event_type: str,
    minute_limit: int,
    hour_limit: int,
) -> bool:
    minute_cutoff = utc_timestamp(now - timedelta(seconds=60))
    hour_cutoff = utc_timestamp(now - timedelta(seconds=3600))

    minute_count = connection.execute(
        """
        SELECT COUNT(*) AS hit_count
        FROM events
        WHERE event_type = ? AND ip_hash = ? AND ts >= ?
        """,
        (event_type, ip_hash, minute_cutoff),
    ).fetchone()["hit_count"]
    if minute_count >= minute_limit:
        return True

    hour_count = connection.execute(
        """
        SELECT COUNT(*) AS hit_count
        FROM events
        WHERE event_type = ? AND ip_hash = ? AND ts >= ?
        """,
        (event_type, ip_hash, hour_cutoff),
    ).fetchone()["hit_count"]
    return hour_count >= hour_limit


def _is_endpoint_rate_limited(
    *,
    connection: sqlite3.Connection,
    endpoint: str,
    ip_hash: str,
    now: datetime,
    minute_limit: int,
    hour_limit: int,
) -> bool:
    minute_cutoff = utc_timestamp(now - timedelta(seconds=60))
    hour_cutoff = utc_timestamp(now - timedelta(seconds=3600))

    minute_count = connection.execute(
        """
        SELECT COUNT(*) AS hit_count
        FROM endpoint_hits
        WHERE endpoint = ? AND ip_hash = ? AND ts >= ?
        """,
        (endpoint, ip_hash, minute_cutoff),
    ).fetchone()["hit_count"]
    if minute_count >= minute_limit:
        return True

    hour_count = connection.execute(
        """
        SELECT COUNT(*) AS hit_count
        FROM endpoint_hits
        WHERE endpoint = ? AND ip_hash = ? AND ts >= ?
        """,
        (endpoint, ip_hash, hour_cutoff),
    ).fetchone()["hit_count"]
    return hour_count >= hour_limit


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


def _cleanup_expired_tokens(connection: sqlite3.Connection, now: datetime) -> None:
    cutoff_ts = utc_timestamp(now)
    connection.execute("DELETE FROM hi_tokens WHERE expires_at <= ?", (cutoff_ts,))


def _cleanup_endpoint_hits(connection: sqlite3.Connection, now: datetime) -> None:
    cutoff_ts = utc_timestamp(now - timedelta(seconds=3600))
    connection.execute("DELETE FROM endpoint_hits WHERE ts < ?", (cutoff_ts,))


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
