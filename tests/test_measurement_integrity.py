from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from backend import db  # noqa: E402
from test_backend_api import (  # noqa: E402
    EVENTS_AUTH_HEADER,
    _clear_managed_runtime_markers,
    _create_compatible_schema,
    _extract_token,
    _make_test_client,
)

CURL_UA = "curl/8.7.1"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Safari/537.36"


class _FakeRequest:
    def __init__(self, headers: dict[str, str], peer_host: str = "10.0.0.9") -> None:
        self.headers = headers
        self.client = type("Client", (), {"host": peer_host})()


@pytest.mark.parametrize(
    ("user_agent", "expected_family"),
    (
        (CURL_UA, db.SELF_TEST_UA_FAMILY),
        ("meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)", "meta_externalagent"),
        ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)", "claude_user"),
        ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) Chrome/145.0 Mobile Safari/537.36 (compatible; GoogleOther)", "googleother"),
        ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)", "applebot"),
        ("python-requests/2.32.0", "python_client"),
        (BROWSER_UA, db.BROWSER_UA_FAMILY),
        ("SomethingNoOneHasSeen/1.0", db.UNKNOWN_UA_FAMILY),
        ("", db.UNKNOWN_UA_FAMILY),
    ),
)
def test_user_agent_family_classification(user_agent: str, expected_family: str) -> None:
    assert db.classify_user_agent(user_agent) == expected_family


def test_likely_crawler_flag_follows_the_family_rules() -> None:
    assert db.detect_likely_crawler("meta-externalagent/1.1") is True
    assert db.detect_likely_crawler("(compatible; GoogleOther)") is True
    assert db.detect_likely_crawler("(Applebot/0.1)") is True
    assert db.detect_likely_crawler("(compatible; Thinkbot/0.5.8)") is True
    # user-triggered assistant fetchers and human clients stay visible
    assert db.detect_likely_crawler("Claude-User/1.0") is False
    assert db.detect_likely_crawler(CURL_UA) is False
    assert db.detect_likely_crawler(BROWSER_UA) is False


def test_trusted_proxy_uses_first_forwarded_hop_then_real_ip_then_peer() -> None:
    forwarded = _FakeRequest({"x-forwarded-for": "203.0.113.5, 10.0.0.1, 172.16.0.1"})
    assert db.extract_client_ip(forwarded, True) == "203.0.113.5"

    real_ip_only = _FakeRequest({"x-real-ip": "198.51.100.7"})
    assert db.extract_client_ip(real_ip_only, True) == "198.51.100.7"

    blank_forwarded = _FakeRequest({"x-forwarded-for": " , "})
    assert db.extract_client_ip(blank_forwarded, True) == "10.0.0.9"

    untrusted = _FakeRequest({"x-forwarded-for": "203.0.113.5"})
    assert db.extract_client_ip(untrusted, False) == "10.0.0.9"


def test_self_test_traffic_is_labelled_manual_and_kept_out_of_the_ratios(client) -> None:
    client.get("/agent.txt", headers={"User-Agent": CURL_UA})
    client.post("/hi", json={}, headers={"User-Agent": CURL_UA})

    payload = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER).json()
    counters = payload["counters"]

    assert counters["hi_total"] == 1
    assert counters["hi_manual"] == 1
    assert counters["hi_unknown"] == 0
    assert counters["self_test_events"] == 2
    assert counters["fetch_excluding_self_test"] == 0
    assert counters["hi_total_excluding_self_test"] == 0
    assert counters["ratio_basis"] == "excluding_self_test"
    assert counters["ratio_total"] == 0.0

    manual_event = payload["events"][0]
    assert manual_event["ua_family"] == db.SELF_TEST_UA_FAMILY
    assert manual_event["source_kind"] == "manual"


def test_non_self_test_follow_through_still_counts_in_the_ratios(
    database_path,
    monkeypatch,
) -> None:
    with _make_test_client(database_path, monkeypatch) as client:
        client.get("/agent.txt", headers={"User-Agent": BROWSER_UA})
        client.get("/hi", headers={"User-Agent": BROWSER_UA})
        payload = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER).json()

    counters = payload["counters"]
    assert counters["fetch_excluding_self_test"] == 1
    assert counters["hi_total_excluding_self_test"] == 1
    assert counters["ratio_total"] == 1.0
    assert counters["self_test_events"] == 0


def test_expired_token_post_is_recorded_as_a_near_miss(database_path, monkeypatch) -> None:
    base_time = datetime(2026, 3, 4, 0, 0, 0, tzinfo=timezone.utc)
    current_time = {"value": base_time}
    monkeypatch.setattr(db, "utc_now", lambda: current_time["value"])

    with _make_test_client(database_path, monkeypatch) as client:
        token = _extract_token(client.get("/agent.txt").text)
        current_time["value"] = base_time + timedelta(seconds=db.TOKEN_TTL_SECONDS + 5)

        late = client.post(
            "/hi",
            json={"agent_name": "Late Scout", "token": token},
            headers={"User-Agent": "python-requests/2.32.0"},
        )
        payload = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER).json()

    assert late.status_code == 400
    assert late.json()["signal"] == "hi_post_expired"
    assert late.json()["hi_post_expired"] == 1

    counters = payload["counters"]
    assert counters["hi_post_expired"] == 1
    assert counters["hi_post_token"] == 0
    assert counters["hi_post"] == 0
    assert counters["hi_total"] == 0

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rejected = connection.execute(
            "SELECT reason, ua_family FROM rejected_tokens ORDER BY id DESC LIMIT 1"
        ).fetchone()

    assert rejected["reason"] == "invalid_or_expired"
    assert rejected["ua_family"] == "python_client"


def test_token_lifetime_is_ten_minutes_and_stated_in_the_fetch_response(
    database_path,
    monkeypatch,
) -> None:
    assert db.TOKEN_TTL_SECONDS == 600

    with _make_test_client(database_path, monkeypatch) as client:
        body = client.get("/agent.txt").text

    assert "This token is valid for 10 minutes." in body


def test_public_feed_exposes_ua_family_without_exposing_the_user_agent(
    database_path,
    monkeypatch,
) -> None:
    with _make_test_client(
        database_path, monkeypatch, events_public_enabled="true"
    ) as client:
        client.get("/llms.txt", headers={"User-Agent": "Mozilla/5.0 (compatible; GoogleOther)"})
        response = client.get("/events/public", params={"type": "all", "limit": 10})

    events = response.json()["events"]
    assert response.status_code == 200
    assert events[0]["ua_family"] == "googleother"
    assert "user_agent" not in events[0]
    assert "ip_hash" not in events[0]


def test_legacy_database_gains_ua_family_column_on_startup(database_path, monkeypatch) -> None:
    _clear_managed_runtime_markers(monkeypatch)
    with sqlite3.connect(database_path) as connection:
        _create_compatible_schema(connection)
        connection.commit()

    with sqlite3.connect(database_path) as connection:
        columns_before = {
            row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
    assert "ua_family" not in columns_before

    with _make_test_client(database_path, monkeypatch) as client:
        client.get("/banana-muffins.md", headers={"User-Agent": "Mozilla/5.0 (compatible; GoogleOther)"})
        payload = client.get("/events", params={"limit": 10}, headers=EVENTS_AUTH_HEADER).json()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        columns_after = {
            row["name"] for row in connection.execute("PRAGMA table_info(events)").fetchall()
        }
        resource_row = connection.execute(
            """
            SELECT ua_family, path
            FROM events
            WHERE event_type = 'resource'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert "ua_family" in columns_after
    assert resource_row["ua_family"] == "googleother"
    assert payload["counters"]["resource"] == 1
