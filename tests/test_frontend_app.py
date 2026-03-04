from __future__ import annotations

import importlib

from streamlit.testing.v1 import AppTest


class _FakeColumn:
    def __init__(self, parent=None) -> None:
        self.parent = parent

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def markdown(self, body: str, unsafe_allow_html: bool = False) -> None:
        if self.parent is not None:
            self.parent.markdown(body, unsafe_allow_html=unsafe_allow_html)

    def caption(self, body: str) -> None:
        if self.parent is not None:
            self.parent.caption_calls.append(body)

    def text(self, body: str) -> None:
        if self.parent is not None:
            self.parent.text_calls.append(body)

    def metric(self, label: str, value, delta=None) -> None:
        if self.parent is not None:
            self.parent.metric_calls.append((label, value, delta))


class _FakeStreamlit:
    def __init__(self) -> None:
        self.session_state: dict[str, object] = {}
        self.page_config_calls: list[dict[str, object]] = []
        self.markdown_calls: list[tuple[str, bool]] = []
        self.write_calls: list[object] = []
        self.caption_calls: list[str] = []
        self.text_calls: list[str] = []
        self.code_calls: list[tuple[str, str | None]] = []
        self.metric_calls: list[tuple[str, object, object]] = []
        self.table_calls: list[object] = []
        self.dataframe_calls: list[tuple[object, bool, bool]] = []
        self.info_calls: list[str] = []
        self.error_calls: list[str] = []

    def set_page_config(self, **kwargs) -> None:
        self.page_config_calls.append(kwargs)

    def markdown(self, body: str, unsafe_allow_html: bool = False) -> None:
        self.markdown_calls.append((body, unsafe_allow_html))

    def caption(self, body: str) -> None:
        self.caption_calls.append(body)

    def text(self, body: str) -> None:
        self.text_calls.append(body)

    def code(self, body: str, language: str | None = None) -> None:
        self.code_calls.append((body, language))

    def metric(self, label: str, value, delta=None) -> None:
        self.metric_calls.append((label, value, delta))

    def table(self, data) -> None:
        self.table_calls.append(data)

    def dataframe(self, data, hide_index: bool = False, use_container_width: bool = False) -> None:
        self.dataframe_calls.append((data, hide_index, use_container_width))

    def subheader(self, _body: str) -> None:
        return None

    def write(self, body) -> None:
        self.write_calls.append(body)

    def info(self, body: str) -> None:
        self.info_calls.append(body)

    def error(self, body: str) -> None:
        self.error_calls.append(body)

    def container(self, border: bool = False):
        return _FakeColumn(self)

    def expander(self, _label: str, expanded: bool = False):
        return _FakeColumn(self)

    def columns(self, _spec, gap=None):
        width = len(_spec) if isinstance(_spec, (list, tuple)) else int(_spec)
        return tuple(_FakeColumn(self) for _ in range(width))


def test_frontend_module_imports_without_running_streamlit_bootstrap() -> None:
    app = importlib.import_module("frontend.app")

    assert callable(app.main)


def test_frontend_runs_with_real_streamlit_testing_and_handles_backend_failure(monkeypatch) -> None:
    monkeypatch.setenv("BACKEND_URL", "http://127.0.0.1:9")

    app_test = AppTest.from_file("frontend/app.py").run(timeout=20)

    assert len(app_test.exception) == 0
    assert len(app_test.error) == 1
    assert app_test.error[0].value == (
        "Could not load backend data: "
        "Live event data is unavailable right now. Check the backend and try again."
    )
    assert len(app_test.metric) >= 4


def test_frontend_main_bootstraps_with_fake_streamlit(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    call_order: list[tuple[str, object]] = []

    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setenv("BACKEND_URL", "http://frontend.test/")

    def record_css() -> None:
        call_order.append(("css", None))

    def record_sidebar(backend_url: str) -> None:
        call_order.append(("sidebar", backend_url))

    def record_controls():
        call_order.append(("controls", None))
        return (
            {
                "route": "All routes",
                "type": "all",
                "source": "all",
                "hide_likely_crawlers": False,
                "q": "",
                "limit": 50,
                "sort_order": "Newest to Oldest",
            },
            False,
            False,
            False,
        )

    def record_sync(backend_url: str, filters: dict[str, object], **kwargs) -> None:
        call_order.append(("sync", backend_url))
        fake_st.session_state["feed_counters"] = {"fetch": 0, "hi_total": 0}
        fake_st.session_state["feed_events"] = []
        fake_st.session_state["feed_has_more"] = False
        fake_st.session_state["feed_error"] = ""
        fake_st.session_state["feed_notice"] = ""
        assert filters["type"] == "all"
        assert kwargs == {
            "refresh_now": False,
            "load_older": False,
            "back_to_newest": False,
        }

    def record_signal_board(counters: dict[str, object]) -> None:
        call_order.append(("signal", counters.get("fetch")))

    def record_ticker(events: list[dict[str, object]]) -> None:
        call_order.append(("ticker", len(events)))

    def record_event_feed(events: list[dict[str, object]]) -> None:
        call_order.append(("feed", len(events)))

    monkeypatch.setattr(app, "_render_css", record_css)
    monkeypatch.setattr(app, "_render_sidebar", record_sidebar)
    monkeypatch.setattr(app, "_render_controls", record_controls)
    monkeypatch.setattr(app, "_sync_feed", record_sync)
    monkeypatch.setattr(app, "_render_signal_board", record_signal_board)
    monkeypatch.setattr(app, "_render_message_ticker", record_ticker)
    monkeypatch.setattr(app, "_render_event_feed", record_event_feed)

    app.main()

    assert fake_st.page_config_calls == [
        {
            "page_title": "agentspotter",
            "layout": "wide",
            "initial_sidebar_state": "collapsed",
        }
    ]
    assert any("agent-spotter" in body for body, _ in fake_st.markdown_calls)
    assert fake_st.session_state["ui_route_filter"] == "All routes"
    assert fake_st.session_state["ui_sort_order"] == "Newest to Oldest"
    assert fake_st.session_state["ui_event_type"] == "all"
    assert fake_st.session_state["ui_limit"] == 50
    assert fake_st.session_state["feed_refresh_cadence_seconds"] == 600
    assert fake_st.session_state["feed_next_refresh_at"] == ""
    assert call_order == [
        ("css", None),
        ("sidebar", "http://frontend.test"),
        ("controls", None),
        ("sync", "http://frontend.test"),
        ("signal", 0),
        ("feed", 0),
        ("ticker", 0),
    ]


def test_manual_curl_snippet_uses_configured_backend_url() -> None:
    app = importlib.import_module("frontend.app")

    snippet = app._manual_curl_snippet("https://backend.example")

    assert "https://backend.example/hi" in snippet
    assert "http://localhost:8000/hi" not in snippet


def test_render_sidebar_mentions_path_specific_reward_and_limitations(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._render_sidebar("https://backend.example")

    markdown_text = " ".join(body for body, _ in fake_st.markdown_calls)
    write_text = " ".join(str(body) for body in fake_st.write_calls)

    assert "https://backend.example/llms.txt" in markdown_text
    assert "your place among callers who used that same response path" in markdown_text
    assert "`hi_post_token`" in markdown_text
    assert "A hi response does not prove that the caller is an AI system" in write_text


def test_display_message_masks_profanity_but_keeps_clean_text() -> None:
    app = importlib.import_module("frontend.app")

    assert app._contains_profanity("This is SHIT.") is True
    assert app._contains_profanity("shiitake mushrooms") is False
    assert app._display_message("  damn  ") == ("****", "contains profanity")
    assert app._display_message("  <script>alert(1)</script>  ") == (
        "<script>alert(1)</script>",
        "",
    )
    assert app._display_message(None) == ("", "")


def test_sync_feed_merges_newer_rows_when_refresh_window_is_open(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._init_state()
    filters = {
        "type": "all",
        "source": "all",
        "hide_likely_crawlers": False,
        "q": "",
        "limit": 2,
    }
    fake_st.session_state["feed_signature"] = app._filter_signature(filters)
    fake_st.session_state["feed_events"] = [
        {"id": 2, "event_type": "hi_get"},
        {"id": 1, "event_type": "fetch"},
    ]
    fake_st.session_state["feed_has_more"] = True
    fake_st.session_state["feed_last_refreshed_at"] = "2026-03-04T00:00:00.000Z"
    fake_st.session_state["feed_next_refresh_at"] = "2026-03-04T00:10:00.000Z"
    monkeypatch.setattr(
        app,
        "_utc_now",
        lambda: app.datetime(2026, 3, 4, 0, 10, 0, tzinfo=app.timezone.utc),
    )
    monkeypatch.setattr(
        app,
        "utc_timestamp",
        lambda value: value.isoformat().replace("+00:00", "Z"),
        raising=False,
    )

    def fake_fetch(*_args, **_kwargs):
        return {
            "refresh": {
                "cadence_seconds": 600,
                "cadence_minutes": 10,
                "last_refreshed_at": "2026-03-04T00:10:00.000Z",
                "next_refresh_at": "2026-03-04T00:20:00.000Z",
            },
            "counters": {
                "fetch": 5,
                "hi_get": 1,
                "hi_post": 0,
                "hi_post_token": 1,
                "hi_total": 2,
                "hi_unknown": 1,
                "hi_manual": 0,
                "hi_agent": 1,
                "ratio_total": 2.5,
                "ratio_unknown": 0.2,
                "fetch_unique_utc_day": 4,
                "hi_total_unique_utc_day": 2,
                "hi_post_token_unique_utc_day": 1,
            },
            "events": [
                {"id": 3, "event_type": "hi_post", "token_used": 1},
                {"id": 2, "event_type": "hi_get"},
            ],
        }

    monkeypatch.setattr(app, "_fetch_events_page", fake_fetch)

    app._sync_feed(
        "https://backend.example",
        filters,
        refresh_now=False,
        load_older=False,
        back_to_newest=False,
    )

    assert [event["id"] for event in fake_st.session_state["feed_events"]] == [3, 2, 1]
    assert fake_st.session_state["feed_notice"] == "Pulled in 1 newer matching events."
    assert fake_st.session_state["feed_counters"]["fetch"] == 5
    assert fake_st.session_state["feed_counters"]["hi_get"] == 1
    assert fake_st.session_state["feed_counters"]["hi_post"] == 0
    assert fake_st.session_state["feed_counters"]["hi_post_token"] == 1
    assert fake_st.session_state["feed_counters"]["hi_total"] == 2
    assert fake_st.session_state["feed_counters"]["hi_agent"] == 1
    assert fake_st.session_state["feed_next_refresh_at"] == "2026-03-04T00:20:00.000Z"


def test_sync_feed_waits_for_countdown_before_refreshing_newer_rows(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._init_state()
    filters = {
        "type": "all",
        "source": "all",
        "hide_likely_crawlers": False,
        "q": "",
        "limit": 2,
    }
    fake_st.session_state["feed_signature"] = app._filter_signature(filters)
    fake_st.session_state["feed_events"] = [
        {"id": 2, "event_type": "hi_post", "token_used": 0},
        {"id": 1, "event_type": "fetch"},
    ]
    fake_st.session_state["feed_last_refreshed_at"] = "2026-03-04T00:00:00.000Z"
    fake_st.session_state["feed_next_refresh_at"] = "2026-03-04T00:10:00.000Z"
    monkeypatch.setattr(
        app,
        "_utc_now",
        lambda: app.datetime(2026, 3, 4, 0, 5, 0, tzinfo=app.timezone.utc),
    )

    called = {"value": False}

    def fake_fetch(*_args, **_kwargs):
        called["value"] = True
        return {"refresh": {}, "counters": {}, "events": []}

    monkeypatch.setattr(app, "_fetch_events_page", fake_fetch)

    app._sync_feed(
        "https://backend.example",
        filters,
        refresh_now=False,
        load_older=False,
        back_to_newest=False,
    )

    assert called["value"] is False
    assert [event["id"] for event in fake_st.session_state["feed_events"]] == [2, 1]


def test_refresh_status_text_reports_last_refresh(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._init_state()
    fake_st.session_state["feed_last_refreshed_at"] = "2026-03-04T00:10:00.000Z"

    status = app._refresh_status_text(
        app.datetime(2026, 3, 4, 0, 0, 1, tzinfo=app.timezone.utc)
    )

    assert "Data refreshed every 10 mins." in status
    assert "Last refreshed on 2026-03-04 00:10:00 UTC." in status


def test_sync_feed_reports_missing_requests_dependency(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)
    monkeypatch.setattr(app, "requests", None)
    monkeypatch.setattr(app, "REQUEST_FETCH_ERRORS", (RuntimeError,))

    app._init_state()
    app._sync_feed(
        "https://backend.example",
        {
            "type": "all",
            "source": "all",
            "hide_likely_crawlers": False,
            "q": "",
            "limit": 2,
        },
        refresh_now=False,
        load_older=False,
        back_to_newest=False,
    )

    assert fake_st.session_state["feed_error"] == (
        "Live event data is unavailable right now. Check the backend and try again."
    )
    assert fake_st.session_state["feed_events"] == []


def test_analysis_snapshot_distinguishes_fetch_get_post_and_token_backed_post() -> None:
    app = importlib.import_module("frontend.app")

    snapshot = app._analysis_snapshot(
        [
            {
                "id": 1,
                "ts": "2026-03-04T00:00:00.000Z",
                "event_type": "fetch",
                "source_kind": "none",
                "user_agent": "Mozilla/5.0",
            },
            {
                "id": 2,
                "ts": "2026-03-04T00:00:30.000Z",
                "event_type": "hi_get",
                "source_kind": "unknown",
                "message": "hello",
                "user_agent": "Mozilla/5.0",
            },
            {
                "id": 3,
                "ts": "2026-03-04T00:01:00.000Z",
                "event_type": "hi_post",
                "token_used": 0,
                "source_kind": "manual",
                "message": "manual hi",
                "user_agent": "curl/8.7.1",
            },
            {
                "id": 4,
                "ts": "2026-03-04T00:01:30.000Z",
                "event_type": "hi_post",
                "token_used": 1,
                "source_kind": "agent",
                "agent_name": "Scout",
                "message": "token hi",
                "user_agent": "python-requests/2.32.0",
            },
        ]
    )

    assert snapshot["sample_fetch"] == 1
    assert snapshot["sample_hi_total"] == 3
    assert snapshot["sample_hi_get"] == 1
    assert snapshot["sample_hi_post"] == 1
    assert snapshot["sample_hi_post_token"] == 1
    assert snapshot["sample_unknown_hi"] == 1


def test_render_signal_board_uses_explicit_counter_labels(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._render_signal_board(
        {
            "fetch": 7,
            "hi_get": 2,
            "hi_post": 2,
            "hi_post_token": 1,
            "hi_total": 5,
            "hi_unknown": 2,
            "hi_manual": 1,
            "hi_agent": 2,
            "ratio_total": 1.4,
            "ratio_unknown": 0.29,
            "fetch_unique_utc_day": 4,
            "hi_total_unique_utc_day": 3,
            "hi_post_token_unique_utc_day": 1,
        }
    )

    labels = [label for label, _value, _delta in fake_st.metric_calls]

    assert "Fetch" in labels
    assert "GET /hi" in labels
    assert "POST /hi" in labels
    assert "POST /hi + token" in labels
    assert "Fetch/Hi_total" in labels
    assert any(
        "higher-confidence follow-through, not verified identity" in body
        for body in fake_st.caption_calls
    )


def test_render_message_ticker_escapes_html_and_masks_profane_messages(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._render_message_ticker(
        [
            {"event_type": "fetch", "agent_name": "ignored", "message": "hello"},
            {"event_type": "hi_get", "agent_name": "Alpha", "message": "hello <b>team</b>"},
            {"event_type": "hi_post", "agent_name": "Bravo", "message": "shit", "token_used": 0},
            {"event_type": "hi_post", "agent_name": "Solo", "message": "   ", "token_used": 1},
            {"event_type": "hi_post", "agent_name": "", "message": "", "token_used": 0},
        ]
    )

    markup = " ".join(body for body, _ in fake_st.markdown_calls)

    assert "Observed Hi Messages" in markup
    assert "Alpha: hello &lt;b&gt;team&lt;/b&gt; via GET /hi" in markup
    assert "Bravo: ****" in markup
    assert "contains profanity" in markup
    assert "Solo said hi via POST /hi + token" in markup
    assert "ignored" not in markup
    assert "shit" not in markup
    assert fake_st.caption_calls[-1] == (
        "Messages from the currently loaded feed; profanity is masked before display. "
        "These are instruction-following signals, not verified identity."
    )


def test_render_event_card_uses_plain_text_for_user_supplied_fields(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._render_event_card(
        {
            "id": 9,
            "ts": "2026-03-04T00:00:00.000Z",
            "event_type": "hi_post",
            "path": "/hi",
            "source_kind": "manual",
            "agent_name": "<b>agent</b>",
            "message": "<script>alert(1)</script>",
            "user_agent": "curl/8.7.1",
            "likely_crawler": False,
            "token_used": 0,
        }
    )

    markdown_text = " ".join(body for body, _ in fake_st.markdown_calls)
    rendered_text = " ".join(fake_st.text_calls)

    assert "<b>agent</b>" in rendered_text
    assert "<script>alert(1)</script>" in rendered_text
    assert "<b>agent</b>" not in markdown_text
    assert "<script>alert(1)</script>" not in markdown_text
    assert "contains profanity" not in fake_st.caption_calls


def test_render_event_card_masks_profane_message(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._render_event_card(
        {
            "id": 10,
            "ts": "2026-03-04T00:00:00.000Z",
            "event_type": "hi_post",
            "path": "/hi",
            "source_kind": "manual",
            "agent_name": "agent",
            "message": "Fuck",
            "user_agent": "curl/8.7.1",
            "likely_crawler": False,
            "token_used": 0,
        }
    )

    markdown_text = " ".join(body for body, _ in fake_st.markdown_calls)
    rendered_text = " ".join(fake_st.text_calls)

    assert "****" in rendered_text
    assert "Fuck" not in rendered_text
    assert "Fuck" not in markdown_text
    assert "contains profanity" in fake_st.caption_calls


def test_render_event_card_labels_token_backed_post_as_higher_confidence_not_verified(
    monkeypatch,
) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._render_event_card(
        {
            "id": 11,
            "ts": "2026-03-04T00:00:00.000Z",
            "event_type": "hi_post",
            "path": "/hi",
            "source_kind": "agent",
            "agent_name": "Scout",
            "message": "hello",
            "user_agent": "python-requests/2.32.0",
            "likely_crawler": False,
            "token_used": 1,
        }
    )

    assert "POST /hi + token" in " ".join(fake_st.text_calls)
    assert "Scout" in " ".join(fake_st.text_calls)
    assert "hello" in " ".join(fake_st.text_calls)


def test_render_event_feed_masks_profane_messages_and_keeps_plain_text_rows(monkeypatch) -> None:
    app = importlib.import_module("frontend.app")
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(app, "st", fake_st)

    app._init_state()
    app._render_event_feed(
        [
            {
                "id": 1,
                "ts": "2026-03-04T00:00:00.000Z",
                "event_type": "hi_post",
                "agent_name": "<b>agent</b>",
                "message": "Fuck",
                "token_used": 0,
            },
            {
                "id": 2,
                "ts": "2026-03-04T00:01:00.000Z",
                "event_type": "hi_get",
                "agent_name": "Viewer",
                "message": "<script>alert(1)</script>",
                "token_used": 0,
            },
        ]
    )

    assert len(fake_st.dataframe_calls) == 1
    rows, hide_index, use_container_width = fake_st.dataframe_calls[0]
    markdown_text = " ".join(body for body, _ in fake_st.markdown_calls)

    assert hide_index is True
    assert use_container_width is True
    assert rows[0]["Name"] == "<b>agent</b>"
    assert rows[0]["Message"] == "****"
    assert rows[1]["Name"] == "Viewer"
    assert rows[1]["Message"] == "<script>alert(1)</script>"
    assert "<b>agent</b>" not in markdown_text
    assert "<script>alert(1)</script>" not in markdown_text
