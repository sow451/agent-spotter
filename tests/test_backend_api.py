from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import RECIPE_PATH, create_app

EVENTS_AUTH_HEADER = {"Authorization": "Bearer frontend-test-token"}
EVENTS_RESPONSE_KEYS = {"refresh", "counters", "events", "has_more"}
PUBLIC_EVENT_KEYS = {"id", "ts", "event_type", "path", "source_kind", "token_used"}
PUBLIC_REFRESH_KEYS = {"cadence_seconds", "cadence_minutes", "last_refreshed_at", "next_refresh_at"}
PUBLIC_COUNTER_KEYS = {
    "resource",
    "fetch",
    "hi_get",
    "hi_post",
    "hi_post_token",
    "hi_total",
    "hi_unknown",
    "hi_manual",
    "hi_agent",
    "fetch_unique_utc_day",
    "hi_total_unique_utc_day",
    "hi_post_token_unique_utc_day",
    "ratio_total",
    "ratio_unknown",
}
INTERNAL_EVENT_KEYS = {
    "id",
    "ts",
    "event_type",
    "path",
    "agent_name",
    "message",
    "source_kind",
    "user_agent",
    "likely_crawler",
    "token_used",
}


def _make_test_client(
    database_path,
    monkeypatch,
    *,
    trust_proxy_headers: str = "false",
    events_public_enabled: str | None = None,
) -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", trust_proxy_headers)
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")
    if events_public_enabled is None:
        monkeypatch.delenv("EVENTS_PUBLIC_ENABLED", raising=False)
    else:
        monkeypatch.setenv("EVENTS_PUBLIC_ENABLED", events_public_enabled)
    return TestClient(create_app())


def _extract_token(agent_txt_body: str) -> str:
    for line in agent_txt_body.splitlines():
        if line.startswith("TOKEN: "):
            return line.split("TOKEN: ", 1)[1].strip()
    raise AssertionError("token line missing from /agent.txt response")


def _clear_managed_runtime_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "RAILWAY_ENVIRONMENT",
        "RAILWAY_PROJECT_ID",
        "RAILWAY_SERVICE_ID",
        "APP_ENV",
        "ENVIRONMENT",
        "ENV",
    ):
        monkeypatch.delenv(key, raising=False)


def _create_compatible_schema(
    connection: sqlite3.Connection,
    *,
    include_event_type_check: bool = True,
    include_resource_event_type: bool = True,
    include_hi_tokens_foreign_key: bool = True,
    include_required_indexes: bool = True,
) -> None:
    event_type_values = "'fetch', 'hi_get', 'hi_post', 'resource'"
    if not include_resource_event_type:
        event_type_values = "'fetch', 'hi_get', 'hi_post'"
    event_type_column = (
        f"event_type TEXT NOT NULL CHECK (event_type IN ({event_type_values}))"
        if include_event_type_check
        else "event_type TEXT NOT NULL"
    )
    foreign_key_clause = (
        ", FOREIGN KEY(fetch_event_id) REFERENCES events(id)"
        if include_hi_tokens_foreign_key
        else ""
    )

    connection.execute(
        f"""
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            {event_type_column},
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
        f"""
        CREATE TABLE hi_tokens (
            token_hash TEXT PRIMARY KEY,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            used_at TEXT,
            fetch_event_id INTEGER NOT NULL,
            issued_ip_hash TEXT NOT NULL
            {foreign_key_clause}
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE source_windows (
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
        CREATE TABLE stats_cache (
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

    if include_required_indexes:
        connection.execute("CREATE INDEX idx_events_ts_desc ON events(ts DESC)")
        connection.execute("CREATE INDEX idx_events_event_id ON events(event_type, id DESC)")
        connection.execute(
            "CREATE INDEX idx_events_ip_event_ts ON events(ip_hash, event_type, ts DESC)"
        )
        connection.execute("CREATE INDEX idx_events_source_id ON events(source_kind, id DESC)")
        connection.execute(
            "CREATE INDEX idx_events_crawler_id ON events(likely_crawler, id DESC)"
        )
        connection.execute("CREATE INDEX idx_hi_tokens_expires ON hi_tokens(expires_at)")


def test_get_agent_txt_returns_token_and_logs_fetch(client, db_connection) -> None:
    response = client.get("/agent.txt", headers={"User-Agent": "ExampleBrowser/1.0"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert RECIPE_PATH.read_text(encoding="utf-8").strip() in response.text
    assert "GET /hi" in response.text
    assert "POST /hi" in response.text
    token = _extract_token(response.text)
    assert token

    event_row = db_connection.execute(
        """
        SELECT event_type, path, agent_name, message, source_kind, user_agent, likely_crawler, token_used
        FROM events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    token_row = db_connection.execute(
        """
        SELECT token_hash, used_at, fetch_event_id
        FROM hi_tokens
        ORDER BY issued_at DESC
        LIMIT 1
        """
    ).fetchone()

    assert event_row["event_type"] == "fetch"
    assert event_row["path"] == "/agent.txt"
    assert event_row["agent_name"] is None
    assert event_row["message"] is None
    assert event_row["source_kind"] == "none"
    assert event_row["user_agent"] == "ExampleBrowser/1.0"
    assert event_row["likely_crawler"] == 0
    assert event_row["token_used"] == 0
    assert token_row["token_hash"] is not None
    assert token_row["used_at"] is None
    assert token_row["fetch_event_id"] == 1


def test_get_agent_txt_marks_known_crawler_user_agent(client, db_connection) -> None:
    response = client.get("/agent.txt", headers={"User-Agent": "GPTBot/1.0"})

    assert response.status_code == 200
    row = db_connection.execute(
        """
        SELECT user_agent, likely_crawler
        FROM events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()

    assert row["user_agent"] == "GPTBot/1.0"
    assert row["likely_crawler"] == 1


def test_health_endpoint_returns_ok_without_recording_events(client, db_connection) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    count_row = db_connection.execute("SELECT COUNT(*) AS hit_count FROM events").fetchone()
    assert count_row["hit_count"] == 0


def test_trust_proxy_headers_uses_forwarded_ip_for_ip_hash(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch, trust_proxy_headers="true") as client:
        response = client.get(
            "/agent.txt",
            headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"},
        )

    assert response.status_code == 200

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT ip_hash
            FROM events
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert row["ip_hash"] == db.hash_ip("203.0.113.5", "test-salt")


def test_llms_and_ai_recipe_routes_serve_invitation_files_and_log_resource_reads(
    client,
    db_connection,
) -> None:
    llms_response = client.get("/llms.txt", headers={"User-Agent": "AgentReader/1.0"})
    ai_recipe_response = client.get("/ai/recipe.md", headers={"User-Agent": "AgentReader/2.0"})

    assert llms_response.status_code == 200
    assert llms_response.headers["content-type"].startswith("text/plain")
    assert "Start here and follow the fetch-flow instructions to get the recipe:" in llms_response.text
    assert "https://agentspotter-backend-production.up.railway.app/ai/recipe.md" in llms_response.text
    assert "Example JSON body for `POST /hi`" in llms_response.text

    assert ai_recipe_response.status_code == 200
    assert ai_recipe_response.headers["content-type"].startswith("text/markdown")
    assert "To retrieve the actual recipe, call:" in ai_recipe_response.text
    assert (
        "`GET https://agentspotter-backend-production.up.railway.app/agent.txt`"
        in ai_recipe_response.text
    )

    rows = db_connection.execute(
        """
        SELECT event_type, path, user_agent, source_kind, token_used
        FROM events
        ORDER BY id ASC
        """
    ).fetchall()

    assert [row["event_type"] for row in rows] == ["resource", "resource"]
    assert [row["path"] for row in rows] == ["/llms.txt", "/ai/recipe.md"]
    assert [row["user_agent"] for row in rows] == ["AgentReader/1.0", "AgentReader/2.0"]
    assert all(row["source_kind"] == "none" for row in rows)
    assert all(row["token_used"] == 0 for row in rows)


def test_canary_recipe_route_serves_markdown_and_logs_resource_read(client, db_connection) -> None:
    response = client.get("/banana-muffins.md", headers={"User-Agent": "CanaryBot/1.0"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "# Banana Muffin Recipe for High Altitude" in response.text
    assert "https://agentspotter-backend-production.up.railway.app/hi" in response.text

    row = db_connection.execute(
        """
        SELECT event_type, path, user_agent, source_kind, token_used
        FROM events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row["event_type"] == "resource"
    assert row["path"] == "/banana-muffins.md"
    assert row["user_agent"] == "CanaryBot/1.0"
    assert row["source_kind"] == "none"
    assert row["token_used"] == 0


def test_get_hi_applies_defaults_and_logs_hi_get(client, db_connection) -> None:
    response = client.get("/hi", headers={"User-Agent": "Browser/1.0"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["signal"] == "hi_get"
    assert payload["hi_total"] == 1
    assert payload["hi_get"] == 1
    assert payload["hi_post"] == 0
    assert payload["hi_post_token"] == 0
    assert payload["ratio_total"] == 0.0
    assert payload["ratio_unknown"] == 0.0
    assert payload["reward_message"] == (
        "You said hi via the easy path. You are the 1st caller on this path."
    )

    row = db_connection.execute(
        """
        SELECT event_type, path, agent_name, message, source_kind, token_used
        FROM events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row["event_type"] == "hi_get"
    assert row["path"] == "/hi"
    assert row["agent_name"] == "anonymous"
    assert row["message"] == "hi"
    assert row["source_kind"] == "unknown"
    assert row["token_used"] == 0


def test_get_hi_accepts_explicit_query_fields_and_logs_values(client, db_connection) -> None:
    response = client.get(
        "/hi",
        params={"agent": "perplexity", "source": "agent", "message": "hello"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["signal"] == "hi_get"

    row = db_connection.execute(
        """
        SELECT agent_name, message, source_kind
        FROM events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row["agent_name"] == "perplexity"
    assert row["message"] == "hello"
    assert row["source_kind"] == "agent"


def test_post_hi_accepts_empty_object_and_applies_defaults(client, db_connection) -> None:
    client.get("/agent.txt")

    response = client.post("/hi", json={})

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "status": "ok",
        "event_id": 2,
        "signal": "hi_post",
        "hi_total": 1,
        "hi_get": 0,
        "hi_post": 1,
        "hi_post_token": 0,
        "ratio_total": 1.0,
        "ratio_unknown": 1.0,
        "reward_message": "You said hi via POST. You are the 1st caller on this path.",
        "token_status": "missing",
    }

    row = db_connection.execute(
        """
        SELECT event_type, agent_name, message, source_kind, token_used
        FROM events
        WHERE event_type = 'hi_post'
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row["agent_name"] == "anonymous"
    assert row["message"] == "hi"
    assert row["source_kind"] == "unknown"
    assert row["token_used"] == 0


def test_post_hi_blank_strings_normalize_to_defaults(client, db_connection) -> None:
    response = client.post(
        "/hi",
        json={"agent_name": "   ", "message": "   ", "source": "   ", "token": "   "},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["signal"] == "hi_post"
    assert payload["token_status"] == "missing"

    row = db_connection.execute(
        """
        SELECT agent_name, message, source_kind, token_used
        FROM events
        ORDER BY id DESC
        LIMIT 1
        """
    ).fetchone()
    assert row["agent_name"] == "anonymous"
    assert row["message"] == "hi"
    assert row["source_kind"] == "unknown"
    assert row["token_used"] == 0


def test_post_hi_accepts_valid_token_and_marks_it_used(client, db_connection) -> None:
    fetch_response = client.get("/agent.txt", headers={"User-Agent": "Browser/1.0"})
    token = _extract_token(fetch_response.text)

    response = client.post(
        "/hi",
        json={"agent_name": "Scout", "source": "agent", "message": "hello", "token": token},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["signal"] == "hi_post_token"
    assert payload["token_status"] == "valid"
    assert payload["hi_total"] == 1
    assert payload["hi_get"] == 0
    assert payload["hi_post"] == 0
    assert payload["hi_post_token"] == 1
    assert payload["ratio_total"] == 1.0
    assert payload["ratio_unknown"] == 0.0
    assert payload["reward_message"] == (
        "You said hi via POST with a valid token. You are the 1st caller on this path."
    )

    event_row = db_connection.execute(
        """
        SELECT event_type, source_kind, token_used
        FROM events
        WHERE id = 2
        """
    ).fetchone()
    token_row = db_connection.execute(
        """
        SELECT used_at
        FROM hi_tokens
        ORDER BY issued_at DESC
        LIMIT 1
        """
    ).fetchone()

    assert event_row["event_type"] == "hi_post"
    assert event_row["source_kind"] == "agent"
    assert event_row["token_used"] == 1
    assert token_row["used_at"] is not None


def test_invalid_token_returns_400_without_writing_events_or_counters(client, db_connection) -> None:
    client.get("/agent.txt")

    response = client.post(
        "/hi",
        json={"agent_name": "Scout", "token": "not-a-real-token"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "status": "invalid_token",
        "token_status": "invalid_or_expired",
        "detail": "Token invalid or expired. Fetch /agent.txt again for a fresh token.",
    }

    hi_rows = db_connection.execute(
        "SELECT COUNT(*) AS hit_count FROM events WHERE event_type IN ('hi_get', 'hi_post')"
    ).fetchone()["hit_count"]
    stats = db_connection.execute(
        """
        SELECT hi_get_count, hi_post_count, hi_post_token_count, hi_total_count
        FROM stats_cache
        WHERE cache_key = 'global'
        """
    ).fetchone()

    assert hi_rows == 0
    assert stats["hi_get_count"] == 0
    assert stats["hi_post_count"] == 0
    assert stats["hi_post_token_count"] == 0
    assert stats["hi_total_count"] == 0


def test_consumed_token_cannot_be_reused(client, db_connection) -> None:
    fetch_response = client.get("/agent.txt")
    token = _extract_token(fetch_response.text)

    first = client.post("/hi", json={"agent_name": "Scout", "token": token})
    second = client.post("/hi", json={"agent_name": "Scout", "token": token})

    assert first.status_code == 200
    assert second.status_code == 400
    assert second.json()["status"] == "invalid_token"

    hi_rows = db_connection.execute(
        "SELECT COUNT(*) AS hit_count FROM events WHERE event_type = 'hi_post'"
    ).fetchone()["hit_count"]
    assert hi_rows == 1


def test_token_expires_at_boundary(database_path, monkeypatch) -> None:
    base_time = datetime(2026, 3, 4, 0, 0, 0, tzinfo=timezone.utc)
    current_time = {"value": base_time}
    monkeypatch.setattr(db, "utc_now", lambda: current_time["value"])

    with _make_test_client(database_path, monkeypatch) as client:
        fetch_response = client.get("/agent.txt")
        token = _extract_token(fetch_response.text)
        current_time["value"] = base_time + timedelta(seconds=60)
        expired = client.post("/hi", json={"agent_name": "Scout", "token": token})

    assert expired.status_code == 400
    assert expired.json()["status"] == "invalid_token"


def test_post_hi_rejects_request_parser_and_validation_edges(client, db_connection) -> None:
    wrong_content_type = client.post("/hi", content="{}", headers={"Content-Type": "text/plain"})
    malformed_json = client.post(
        "/hi",
        content='{"agent_name":',
        headers={"Content-Type": "application/json"},
    )
    non_object_json = client.post(
        "/hi",
        content='["not-an-object"]',
        headers={"Content-Type": "application/json"},
    )
    oversized = client.post(
        "/hi",
        content='{"message":"' + ("x" * 1100) + '"}',
        headers={"Content-Type": "application/json"},
    )
    bad_source = client.get("/hi", params={"source": "robot"})
    non_string_token = client.post("/hi", json={"token": 123, "agent_name": "Scout"})
    long_message = client.post("/hi", json={"message": "x" * 281, "agent_name": "Scout"})

    assert wrong_content_type.status_code == 400
    assert wrong_content_type.json() == {"detail": "application/json required"}
    assert malformed_json.status_code == 400
    assert malformed_json.json() == {"detail": "malformed JSON"}
    assert non_object_json.status_code == 400
    assert non_object_json.json() == {"detail": "JSON object required"}
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "request body too large"}
    assert bad_source.status_code == 400
    assert bad_source.json() == {"detail": "source must be unknown, manual, or agent"}
    assert non_string_token.status_code == 400
    assert non_string_token.json() == {"detail": "token must be a string"}
    assert long_message.status_code == 400
    assert long_message.json() == {"detail": "message must be 280 chars or fewer"}

    hi_rows = db_connection.execute(
        "SELECT COUNT(*) AS hit_count FROM events WHERE event_type IN ('hi_get', 'hi_post')"
    ).fetchone()["hit_count"]
    assert hi_rows == 0


def test_post_hi_rejects_invalid_agent_name_types_and_lengths(client, db_connection) -> None:
    non_string_agent = client.post("/hi", json={"agent_name": 123})
    long_agent_name = client.post("/hi", json={"agent_name": "x" * 81})

    assert non_string_agent.status_code == 400
    assert non_string_agent.json() == {"detail": "agent_name must be a string"}
    assert long_agent_name.status_code == 400
    assert long_agent_name.json() == {"detail": "agent_name must be 80 chars or fewer"}

    hi_rows = db_connection.execute(
        "SELECT COUNT(*) AS hit_count FROM events WHERE event_type IN ('hi_get', 'hi_post')"
    ).fetchone()["hit_count"]
    assert hi_rows == 0


def test_create_app_requires_explicit_salt(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.delenv("SALT", raising=False)
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")

    with pytest.raises(RuntimeError, match="SALT is required"):
        create_app()


def test_create_app_requires_explicit_proxy_setting(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    with pytest.raises(RuntimeError, match="TRUST_PROXY_HEADERS is required"):
        create_app()

    monkeypatch.setenv("TRUST_PROXY_HEADERS", "maybe")

    with pytest.raises(RuntimeError, match="TRUST_PROXY_HEADERS must be explicitly set to true or false"):
        create_app()


def test_create_app_requires_frontend_api_token(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.delenv("FRONTEND_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="FRONTEND_API_TOKEN is required"):
        create_app()


def test_create_app_defaults_events_public_enabled_to_false_when_unset(
    database_path,
    monkeypatch,
) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")
    monkeypatch.delenv("EVENTS_PUBLIC_ENABLED", raising=False)

    app = create_app()

    assert app.state.events_public_enabled is False


def test_create_app_rejects_invalid_events_public_enabled_value(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")
    monkeypatch.setenv("EVENTS_PUBLIC_ENABLED", "definitely")

    with pytest.raises(RuntimeError, match="EVENTS_PUBLIC_ENABLED must be true or false when set"):
        create_app()


def test_create_app_requires_database_path_in_managed_runtime(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")

    with pytest.raises(RuntimeError, match="DATABASE_PATH is required in managed runtime"):
        create_app()


def test_create_app_allows_default_database_path_for_local_dev(tmp_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_PATH", raising=False)
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")

    app = create_app()

    assert app.state.database_path == "events.db"
    assert (tmp_path / "events.db").exists()


def test_post_hi_rate_limits_after_three_successes_per_minute(client, db_connection) -> None:
    for index in range(3):
        response = client.post("/hi", json={"agent_name": f"client-{index}"})
        assert response.status_code == 200

    blocked = client.post("/hi", json={"agent_name": "client-3"})

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}

    hi_post_count = db_connection.execute(
        "SELECT COUNT(*) AS hit_count FROM events WHERE event_type = 'hi_post'"
    ).fetchone()["hit_count"]
    assert hi_post_count == 3


def test_post_hi_rate_limits_after_twenty_successes_per_hour(database_path, monkeypatch) -> None:
    base_time = datetime(2026, 3, 4, 0, 0, 0, tzinfo=timezone.utc)
    current_time = {"value": base_time}
    monkeypatch.setattr(db, "utc_now", lambda: current_time["value"])

    with _make_test_client(database_path, monkeypatch) as client:
        for index in range(20):
            current_time["value"] = base_time + timedelta(seconds=61 * index)
            response = client.post("/hi", json={"agent_name": f"hourly-{index}"})
            assert response.status_code == 200

        current_time["value"] = base_time + timedelta(seconds=61 * 20)
        blocked = client.post("/hi", json={"agent_name": "hourly-20"})

    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}


def test_get_agent_txt_rate_limits_after_two_successes_per_minute(database_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "FETCH_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(db, "FETCH_RATE_LIMIT_PER_HOUR", 20)

    with _make_test_client(database_path, monkeypatch) as client:
        first = client.get("/agent.txt")
        second = client.get("/agent.txt")
        blocked = client.get("/agent.txt")

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        fetch_count = connection.execute(
            "SELECT COUNT(*) AS hit_count FROM events WHERE event_type = 'fetch'"
        ).fetchone()["hit_count"]
        token_count = connection.execute(
            "SELECT COUNT(*) AS hit_count FROM hi_tokens"
        ).fetchone()["hit_count"]

    assert fetch_count == 2
    assert token_count == 2


def test_get_hi_rate_limits_after_two_successes_per_minute(database_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "HI_GET_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(db, "HI_GET_RATE_LIMIT_PER_HOUR", 20)

    with _make_test_client(database_path, monkeypatch) as client:
        first = client.get("/hi")
        second = client.get("/hi")
        blocked = client.get("/hi")

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        hi_get_count = connection.execute(
            "SELECT COUNT(*) AS hit_count FROM events WHERE event_type = 'hi_get'"
        ).fetchone()["hit_count"]

    assert hi_get_count == 2


def test_write_paths_update_stats_without_full_rebuild_in_hot_path(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch) as client:
        def _fail_if_called(*_args, **_kwargs):
            raise AssertionError("full stats rebuild should not run during accepted writes")

        monkeypatch.setattr(db, "rebuild_stats_cache", _fail_if_called)

        fetch_response = client.get("/agent.txt")
        token = _extract_token(fetch_response.text)
        hi_get_response = client.get("/hi", params={"source": "manual"})
        hi_post_token_response = client.post(
            "/hi",
            json={"agent_name": "Scout", "source": "agent", "token": token},
        )
        events_response = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)

    assert fetch_response.status_code == 200
    assert hi_get_response.status_code == 200
    assert hi_post_token_response.status_code == 200
    assert events_response.status_code == 200
    counters = events_response.json()["counters"]
    assert counters["fetch"] == 1
    assert counters["hi_get"] == 1
    assert counters["hi_post"] == 0
    assert counters["hi_post_token"] == 1
    assert counters["hi_total"] == 2


def test_get_events_rate_limits_after_two_successes_per_minute(database_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "EVENTS_RATE_LIMIT_PER_MINUTE", 2)
    monkeypatch.setattr(db, "EVENTS_RATE_LIMIT_PER_HOUR", 20)

    with _make_test_client(database_path, monkeypatch) as client:
        first = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)
        second = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)
        blocked = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)

    assert first.status_code == 200
    assert second.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json() == {"detail": "rate limit exceeded"}

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        hit_count = connection.execute(
            """
            SELECT COUNT(*) AS hit_count
            FROM endpoint_hits
            WHERE endpoint = '/events'
            """
        ).fetchone()["hit_count"]
    assert hit_count == 2


def test_fetch_path_cleans_up_expired_tokens(database_path, monkeypatch) -> None:
    base_time = datetime(2026, 3, 4, 0, 0, 0, tzinfo=timezone.utc)
    current_time = {"value": base_time}
    monkeypatch.setattr(db, "utc_now", lambda: current_time["value"])

    with _make_test_client(database_path, monkeypatch) as client:
        first_fetch = client.get("/agent.txt")
        first_token = _extract_token(first_fetch.text)

        current_time["value"] = base_time + timedelta(seconds=61)
        second_fetch = client.get("/agent.txt")
        second_token = _extract_token(second_fetch.text)

    assert first_fetch.status_code == 200
    assert second_fetch.status_code == 200

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        token_rows = connection.execute(
            "SELECT token_hash FROM hi_tokens ORDER BY issued_at ASC"
        ).fetchall()

    assert len(token_rows) == 1
    assert token_rows[0]["token_hash"] == db._hash_token(second_token)
    assert token_rows[0]["token_hash"] != db._hash_token(first_token)


def test_get_events_exposes_revised_counters_and_hides_private_fields(client) -> None:
    fetch_response = client.get("/agent.txt", headers={"User-Agent": "Browser/1.0"})
    token = _extract_token(fetch_response.text)
    client.get(
        "/hi",
        params={"agent": "browser-reader", "message": "hello"},
        headers={"User-Agent": "Browser/1.0"},
    )
    client.post(
        "/hi",
        json={"agent_name": "Manual One", "source": "manual", "message": "checking in"},
        headers={"User-Agent": "curl/8.7.1"},
    )
    client.post(
        "/hi",
        json={"agent_name": "Scout", "source": "agent", "message": "token hi", "token": token},
        headers={"User-Agent": "python-requests/2.32.0"},
    )

    response = client.get(
        "/events",
        params={"type": "hi", "source": "agent", "q": "token", "limit": 10},
        headers=EVENTS_AUTH_HEADER,
    )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == {"refresh", "counters", "events", "has_more"}
    assert payload["has_more"] is False
    assert payload["counters"]["fetch"] == 1
    assert payload["counters"]["hi_get"] == 1
    assert payload["counters"]["hi_post"] == 1
    assert payload["counters"]["hi_post_token"] == 1
    assert payload["counters"]["hi_total"] == 3
    assert payload["counters"]["hi_unknown"] == 1
    assert payload["counters"]["hi_manual"] == 1
    assert payload["counters"]["hi_agent"] == 1
    assert payload["counters"]["ratio_total"] == 0.3333
    assert payload["counters"]["ratio_unknown"] == 1.0

    events = payload["events"]
    assert len(events) == 1
    assert events[0]["event_type"] == "hi_post"
    assert events[0]["source_kind"] == "agent"
    assert events[0]["token_used"] is True
    assert "ip_hash" not in events[0]
    assert "token" not in events[0]


def test_get_events_exposes_ten_minute_refresh_contract(database_path, monkeypatch) -> None:
    current_time = {"value": datetime(2026, 3, 4, 5, 6, 7, tzinfo=timezone.utc)}
    monkeypatch.setattr(db, "utc_now", lambda: current_time["value"])

    with _make_test_client(database_path, monkeypatch) as client:
        client.get("/agent.txt")
        response = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)

    assert response.status_code == 200
    assert response.json()["refresh"] == {
        "cadence_seconds": 600,
        "cadence_minutes": 10,
        "last_refreshed_at": "2026-03-04T05:06:07.000Z",
        "next_refresh_at": "2026-03-04T05:10:00.000Z",
    }


def test_startup_rebuild_recovers_missing_stats_cache(client, database_path, db_connection, monkeypatch) -> None:
    client.get("/agent.txt")
    client.get("/hi", params={"agent": "reader"})
    client.post("/hi", json={"agent_name": "Scout", "source": "agent"})

    db_connection.execute("DELETE FROM stats_cache")
    db_connection.commit()

    with _make_test_client(database_path, monkeypatch) as rebuilt_client:
        response = rebuilt_client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)

    counters = response.json()["counters"]
    assert counters["fetch"] == 1
    assert counters["hi_get"] == 1
    assert counters["hi_post"] == 1
    assert counters["hi_post_token"] == 0
    assert counters["hi_total"] == 2


def test_startup_rejects_unversioned_legacy_database(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY)")
        connection.execute("CREATE TABLE source_windows (window_day TEXT)")
        connection.execute("CREATE TABLE stats_cache (cache_key TEXT)")
        connection.commit()

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")

    with pytest.raises(db.SchemaCompatibilityError, match="incompatible database schema detected"):
        create_app()


def test_startup_rejects_malformed_hi_tokens_table(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                event_type TEXT,
                path TEXT,
                agent_name TEXT,
                message TEXT,
                source_kind TEXT,
                user_agent TEXT,
                ip_hash TEXT,
                likely_crawler INTEGER,
                token_used INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE source_windows (
                window_day TEXT,
                event_type TEXT,
                source_kind TEXT,
                token_used INTEGER,
                ip_hash TEXT,
                event_count INTEGER,
                first_ts TEXT,
                last_ts TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE stats_cache (
                cache_key TEXT,
                cache_window_day TEXT,
                fetch_count INTEGER,
                hi_get_count INTEGER,
                hi_post_count INTEGER,
                hi_post_token_count INTEGER,
                hi_total_count INTEGER,
                hi_unknown_count INTEGER,
                hi_manual_count INTEGER,
                fetch_unique_utc_day INTEGER,
                hi_total_unique_utc_day INTEGER,
                hi_post_token_unique_utc_day INTEGER,
                updated_at TEXT
            )
            """
        )
        connection.execute("CREATE TABLE hi_tokens (token_hash TEXT PRIMARY KEY)")
        connection.commit()

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")

    with pytest.raises(db.SchemaCompatibilityError, match="incompatible database schema detected"):
        create_app()


def test_startup_rejects_schema_missing_required_indexes(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        _create_compatible_schema(connection, include_required_indexes=False)
        connection.commit()

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")

    with pytest.raises(db.SchemaCompatibilityError, match="missing required indexes"):
        create_app()


def test_startup_rejects_schema_with_missing_hi_token_foreign_key(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        _create_compatible_schema(connection, include_hi_tokens_foreign_key=False)
        connection.commit()

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")

    with pytest.raises(db.SchemaCompatibilityError, match="foreign key"):
        create_app()


def test_startup_rejects_schema_with_missing_event_type_check(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        _create_compatible_schema(connection, include_event_type_check=False)
        connection.commit()

    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")

    with pytest.raises(db.SchemaCompatibilityError, match="CHECK constraint"):
        create_app()


def test_legacy_event_type_check_uses_fallback_table_for_resource_reads(
    database_path,
    monkeypatch,
) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        _create_compatible_schema(connection, include_resource_event_type=False)
        connection.commit()

    with _make_test_client(database_path, monkeypatch) as client:
        response = client.get("/llms.txt", headers={"User-Agent": "LegacyAgent/1.0"})

    assert response.status_code == 200

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        resource_event_count = connection.execute(
            "SELECT COUNT(*) AS hit_count FROM events WHERE event_type = 'resource'"
        ).fetchone()["hit_count"]
        fallback_row = connection.execute(
            """
            SELECT path, user_agent, likely_crawler
            FROM resource_reads
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert resource_event_count == 0
    assert fallback_row["path"] == "/llms.txt"
    assert fallback_row["user_agent"] == "LegacyAgent/1.0"
    assert fallback_row["likely_crawler"] == 0


def test_get_events_validates_filters_and_preserves_fetch_rows_when_source_filtered(client) -> None:
    client.get("/agent.txt")
    client.post("/hi", json={"agent_name": "Manual One", "source": "manual"})
    client.post("/hi", json={"agent_name": "Scout", "source": "agent"})

    invalid_type = client.get("/events", params={"type": "bogus"}, headers=EVENTS_AUTH_HEADER)
    invalid_source = client.get("/events", params={"source": "bogus"}, headers=EVENTS_AUTH_HEADER)
    filtered = client.get(
        "/events",
        params={"type": "all", "source": "agent", "limit": 10},
        headers=EVENTS_AUTH_HEADER,
    )

    assert invalid_type.status_code == 400
    assert invalid_type.json() == {"detail": "invalid type"}
    assert invalid_source.status_code == 400
    assert invalid_source.json() == {"detail": "invalid source"}
    assert filtered.status_code == 200
    events = filtered.json()["events"]
    assert any(event["event_type"] == "fetch" for event in events)
    assert any(
        event["event_type"] == "hi_post" and event["source_kind"] == "agent"
        for event in events
    )
    assert all(
        event["event_type"] == "fetch"
        or event["source_kind"] == "agent"
        for event in events
    )


def test_get_events_can_hide_likely_crawler_rows(client) -> None:
    client.get("/agent.txt", headers={"User-Agent": "GPTBot/1.0"})
    client.get("/agent.txt", headers={"User-Agent": "Mozilla/5.0"})

    response = client.get(
        "/events",
        params={"hide_likely_crawlers": True, "limit": 10},
        headers=EVENTS_AUTH_HEADER,
    )

    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert events[0]["user_agent"] == "Mozilla/5.0"
    assert events[0]["likely_crawler"] is False


def test_get_events_requires_frontend_api_token(client) -> None:
    missing = client.get("/events", params={"limit": 10})
    invalid = client.get(
        "/events",
        params={"limit": 10},
        headers={"Authorization": "Bearer wrong-token"},
    )
    valid = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)

    assert missing.status_code == 401
    assert missing.json() == {"detail": "unauthorized"}
    assert invalid.status_code == 401
    assert invalid.json() == {"detail": "unauthorized"}
    assert valid.status_code == 200


def test_get_public_events_returns_503_when_disabled(client) -> None:
    response = client.get("/events/public")

    assert response.status_code == 503
    assert response.json() == {"detail": "public events feed is disabled"}


def test_get_public_events_includes_cors_header_for_browser_fetches(
    database_path,
    monkeypatch,
) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        response = client.get(
            "/events/public",
            params={"limit": 10},
            headers={"Origin": "https://sowrao.com"},
        )

    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == "*"


def test_get_public_events_returns_allowlist_shape(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        fetch_response = client.get("/agent.txt")
        token = _extract_token(fetch_response.text)
        client.get("/hi", params={"agent": "reader", "source": "manual", "message": "manual hi"})
        client.post("/hi", json={"agent_name": "Scout", "source": "agent", "token": token})

        response = client.get(
            "/events/public",
            params={"type": "all", "source": "all", "hide_likely_crawlers": True, "limit": 10},
        )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == EVENTS_RESPONSE_KEYS
    assert set(payload["refresh"].keys()) == PUBLIC_REFRESH_KEYS
    assert set(payload["counters"].keys()) == PUBLIC_COUNTER_KEYS
    assert isinstance(payload["events"], list)
    assert isinstance(payload["has_more"], bool)


def test_resource_counter_counts_only_banana_recipe_reads(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        client.get("/llms.txt", headers={"User-Agent": "Reader/1.0"})
        client.get("/ai/recipe.md", headers={"User-Agent": "Reader/2.0"})
        client.get("/banana-muffins.md", headers={"User-Agent": "Reader/3.0"})

        internal_response = client.get("/events", params={"limit": 20}, headers=EVENTS_AUTH_HEADER)
        public_response = client.get("/events/public", params={"limit": 20})

    assert internal_response.status_code == 200
    assert public_response.status_code == 200

    internal_payload = internal_response.json()
    public_payload = public_response.json()

    assert internal_payload["counters"]["resource"] == 1
    assert public_payload["counters"]["resource"] == 1

    internal_resource_events = [
        event for event in internal_payload["events"] if event["event_type"] == "resource"
    ]
    public_resource_events = [
        event for event in public_payload["events"] if event["event_type"] == "resource"
    ]

    assert len(internal_resource_events) == 3
    assert len(public_resource_events) == 3
    assert {event["path"] for event in internal_resource_events} == {
        "/llms.txt",
        "/ai/recipe.md",
        "/banana-muffins.md",
    }


def test_get_public_events_rate_limits_independently_from_internal_events(database_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "EVENTS_PUBLIC_RATE_LIMIT_PER_MINUTE", 1)
    monkeypatch.setattr(db, "EVENTS_PUBLIC_RATE_LIMIT_PER_HOUR", 20)
    monkeypatch.setattr(db, "EVENTS_RATE_LIMIT_PER_MINUTE", 5)
    monkeypatch.setattr(db, "EVENTS_RATE_LIMIT_PER_HOUR", 20)

    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        public_first = client.get("/events/public", params={"limit": 10})
        public_blocked = client.get("/events/public", params={"limit": 10})
        internal_ok = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER)

    assert public_first.status_code == 200
    assert public_blocked.status_code == 429
    assert public_blocked.json() == {"detail": "rate limit exceeded"}
    assert internal_ok.status_code == 200

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        public_hit_count = connection.execute(
            """
            SELECT COUNT(*) AS hit_count
            FROM endpoint_hits
            WHERE endpoint = '/events/public'
            """
        ).fetchone()["hit_count"]
        internal_hit_count = connection.execute(
            """
            SELECT COUNT(*) AS hit_count
            FROM endpoint_hits
            WHERE endpoint = '/events'
            """
        ).fetchone()["hit_count"]

    assert public_hit_count == 1
    assert internal_hit_count == 1


def test_get_public_events_hides_forbidden_event_fields(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        client.post(
            "/hi",
            json={"agent_name": "Manual One", "source": "manual", "message": "checking in"},
            headers={"User-Agent": "curl/8.7.1"},
        )
        response = client.get("/events/public", params={"type": "hi", "source": "manual", "limit": 10})

    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1
    assert set(events[0].keys()) == PUBLIC_EVENT_KEYS
    assert "agent_name" not in events[0]
    assert "message" not in events[0]
    assert "user_agent" not in events[0]
    assert "likely_crawler" not in events[0]


def test_get_public_events_enforces_strict_schema_for_multiple_event_types(
    database_path,
    monkeypatch,
) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        fetch_response = client.get("/agent.txt", headers={"User-Agent": "Browser/1.0"})
        token = _extract_token(fetch_response.text)
        client.get("/llms.txt", headers={"User-Agent": "ResourceReader/1.0"})
        client.get(
            "/hi",
            params={"agent": "reader", "source": "manual", "message": "manual hi"},
            headers={"User-Agent": "Browser/1.0"},
        )
        client.post(
            "/hi",
            json={"agent_name": "Scout", "source": "agent", "message": "token hi", "token": token},
            headers={"User-Agent": "python-requests/2.32.0"},
        )
        response = client.get(
            "/events/public",
            params={"type": "all", "source": "all", "hide_likely_crawlers": True, "limit": 20},
        )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload.keys()) == EVENTS_RESPONSE_KEYS

    events = payload["events"]
    assert {"fetch", "hi_get", "hi_post", "resource"} <= {event["event_type"] for event in events}
    assert any(event["event_type"] == "hi_post" and event["token_used"] is True for event in events)
    assert all(set(event.keys()) == PUBLIC_EVENT_KEYS for event in events)


def test_get_public_events_caps_limit_to_fifty(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        for _ in range(55):
            client.get("/agent.txt")
        response = client.get("/events/public", params={"limit": 500})

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["events"]) == 50
    assert payload["has_more"] is True


def test_get_public_events_validates_filters_and_rejects_disabled_query_fields(
    database_path,
    monkeypatch,
) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        invalid_type = client.get("/events/public", params={"type": "bogus"})
        invalid_source = client.get("/events/public", params={"source": "bogus"})
        unsupported_q = client.get("/events/public", params={"q": "token"})
        unsupported_before_id = client.get("/events/public", params={"before_id": 123})

    assert invalid_type.status_code == 400
    assert invalid_type.json() == {"detail": "invalid type"}
    assert invalid_source.status_code == 400
    assert invalid_source.json() == {"detail": "invalid source"}
    assert unsupported_q.status_code == 400
    assert unsupported_q.json() == {"detail": "q is not supported on /events/public"}
    assert unsupported_before_id.status_code == 400
    assert unsupported_before_id.json() == {"detail": "before_id is not supported on /events/public"}


def test_get_public_events_does_not_require_auth(database_path, monkeypatch) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        no_auth = client.get("/events/public", params={"limit": 10})
        wrong_auth = client.get(
            "/events/public",
            params={"limit": 10},
            headers={"Authorization": "Bearer wrong-token"},
        )

    assert no_auth.status_code == 200
    assert wrong_auth.status_code == 200


def test_internal_events_contract_unchanged_when_public_events_enabled(
    database_path,
    monkeypatch,
) -> None:
    with _make_test_client(database_path, monkeypatch, events_public_enabled="true") as client:
        client.post(
            "/hi",
            json={"agent_name": "Manual One", "source": "manual", "message": "checking in"},
            headers={"User-Agent": "curl/8.7.1"},
        )
        public_response = client.get("/events/public", params={"type": "hi", "source": "manual", "limit": 10})
        internal_unauthorized = client.get("/events", params={"type": "hi", "source": "manual", "limit": 10})
        internal_authorized = client.get(
            "/events",
            params={"type": "hi", "source": "manual", "limit": 10},
            headers=EVENTS_AUTH_HEADER,
        )

    assert public_response.status_code == 200
    assert internal_unauthorized.status_code == 401
    assert internal_unauthorized.json() == {"detail": "unauthorized"}
    assert internal_authorized.status_code == 200

    public_event = public_response.json()["events"][0]
    internal_event = internal_authorized.json()["events"][0]

    assert set(public_event.keys()) == PUBLIC_EVENT_KEYS
    assert set(internal_event.keys()) == INTERNAL_EVENT_KEYS
    assert set(public_event.keys()) < set(internal_event.keys())


def test_get_events_rejects_malformed_authorization_headers(client) -> None:
    malformed_authorization_headers = [
        {"Authorization": "Basic frontend-test-token"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer   "},
        {"Authorization": "Bearer: frontend-test-token"},
        {"Authorization": "Bearer\tfrontend-test-token"},
        {"Authorization": "Bearer,frontend-test-token"},
        {"Authorization": "Bearer frontend-test-token extra"},
    ]

    for headers in malformed_authorization_headers:
        response = client.get("/events", params={"limit": 10}, headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "unauthorized"}
