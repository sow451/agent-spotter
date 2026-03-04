from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend import db
from backend.main import RECIPE_PATH, create_app

EVENTS_AUTH_HEADER = {"Authorization": "Bearer frontend-test-token"}


def _make_test_client(database_path, monkeypatch, *, trust_proxy_headers: str = "false") -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", trust_proxy_headers)
    monkeypatch.setenv("FRONTEND_API_TOKEN", "frontend-test-token")
    return TestClient(create_app())


def _extract_token(agent_txt_body: str) -> str:
    for line in agent_txt_body.splitlines():
        if line.startswith("TOKEN: "):
            return line.split("TOKEN: ", 1)[1].strip()
    raise AssertionError("token line missing from /agent.txt response")


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


def test_llms_and_ai_recipe_routes_serve_invitation_files(client) -> None:
    llms_response = client.get("/llms.txt")
    ai_recipe_response = client.get("/ai/recipe.md")

    assert llms_response.status_code == 200
    assert llms_response.headers["content-type"].startswith("text/plain")
    assert "Start here: `/ai/recipe.md`" in llms_response.text

    assert ai_recipe_response.status_code == 200
    assert ai_recipe_response.headers["content-type"].startswith("text/markdown")
    assert "To retrieve the actual recipe, call:" in ai_recipe_response.text
    assert "`GET /agent.txt`" in ai_recipe_response.text


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
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.delenv("SALT", raising=False)
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")

    with pytest.raises(RuntimeError, match="SALT is required"):
        create_app()


def test_create_app_requires_explicit_proxy_setting(database_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)

    with pytest.raises(RuntimeError, match="TRUST_PROXY_HEADERS is required"):
        create_app()

    monkeypatch.setenv("TRUST_PROXY_HEADERS", "maybe")

    with pytest.raises(RuntimeError, match="TRUST_PROXY_HEADERS must be explicitly set to true or false"):
        create_app()


def test_create_app_requires_frontend_api_token(database_path, monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(database_path))
    monkeypatch.setenv("SALT", "test-salt")
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "false")
    monkeypatch.delenv("FRONTEND_API_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="FRONTEND_API_TOKEN is required"):
        create_app()


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
