# Test Scenarios for `agentspotter`

This project is a small public experiment. The test suite checks that the app records the revised signal ladder honestly:

- `fetch` from `GET /agent.txt`
- `hi_get` from `GET /hi`
- `hi_post` from `POST /hi` without a token
- `hi_post_token` from `POST /hi` with a valid token

The goal is not to prove identity. The goal is to make sure the experiment logs the right thing, counts the right thing, and displays it safely.

## Latest Automated Run

- Run completed: 2026-03-04 18:19:34 IST (Asia/Kolkata)
- Command used: `./.venv/bin/python -m pytest -q`
- Result: `42 passed`
- Failures: none

## Backend Scenarios Covered

- `GET /agent.txt` returns machine-readable instructions, issues a one-time token, logs `fetch`, and stores the token server-side.
- Known crawler user agents are flagged as likely crawlers, and trusted proxy mode uses the first `X-Forwarded-For` hop for IP hashing.
- `GET /hi` works as the easy fallback path and applies defaults for omitted `agent`, `source`, and `message`.
- `GET /hi` also covers the explicit documented path with `agent`, `source`, and `message` query params.
- `POST /hi` accepts `{}` as valid input, applies defaults, and returns the revised response shape with `signal`, counters, ratios, and `reward_message`.
- Blank-string optional fields normalize back to defaults, and invalid `agent_name` types/lengths are rejected.
- `POST /hi` with a valid token is recorded as token-backed follow-through and marks the token as used.
- Invalid tokens return `400` and do not create events or increment counters.
- Reusing a token after it has already been consumed is rejected.
- A token is rejected exactly at the expiry boundary (`current_time == expires_at`).
- Rate limits still apply to successful `POST /hi` writes for both the 1-minute and 1-hour windows.
- Parser and validation failure branches are covered for wrong content type, malformed JSON, non-object JSON, oversized bodies, invalid `source`, non-string `token`, and overlong `message`.
- `/events` exposes the revised public counters (`hi_get`, `hi_post`, `hi_post_token`, `hi_total`, `hi_unknown`, `hi_manual`, `hi_agent`, `ratio_total`, `ratio_unknown`) plus refresh metadata.
- `/events` rows include `token_used` and still do not expose private fields such as IP hashes or token material.
- `/events` rejects invalid filters and preserves fetch rows when `type=all` is combined with a `source` filter.
- `/events` can hide likely crawler rows from the visible feed while preserving the global counters contract.
- Startup still fails fast when `SALT` or `TRUST_PROXY_HEADERS` are missing or invalid.
- Legacy/incompatible SQLite files are rejected instead of being silently accepted.
- A malformed preexisting `hi_tokens` table is rejected at startup instead of failing later during token issuance.
- Rebuilding a missing `stats_cache` row reconstructs counters from stored events.

## Frontend Scenarios Covered

- `frontend/app.py` still imports safely without auto-running the page.
- `main()` bootstraps with the fake Streamlit harness and runs the expected render flow.
- The refresh/countdown path still works with the server-owned 10-minute refresh envelope.
- The dashboard normalizes and displays the revised counter set, including the split between `GET /hi`, `POST /hi`, and token-backed `POST /hi`.
- The UI still treats token-backed POSTs as higher-confidence follow-through, not verified identity.
- The sidebar copy is regression-tested for the path-specific reward promise and the explicit non-identity limitation.
- The ticker still escapes user content, masks profanity, and uses the newer event shapes.
- Event cards still render user-provided text through plain-text paths rather than markdown.
- The main event table also masks profane messages and keeps user-provided values in plain-text dataframe rows.
- There is now one real `streamlit.testing` smoke test that runs the actual app and confirms it renders without exceptions even when backend fetches fail.

## Remaining Gaps

- Most frontend tests still use the fake harness; we have one real Streamlit smoke test now, but not broad real-widget interaction coverage.
- We still do not cover every optional-field edge case (for example every bad type/length combination for every field on both GET and POST).
- We still do not have browser-level verification of the rendered page.

## Practical Next Tests

1. Add direct positive-path frontend coverage for the real auto-refresh timer, including the computed delay and emitted reload script.
2. Add backend coverage proving repeated requests from the same source do not inflate the UTC-day unique counters.
3. Tighten legacy database compatibility checks and add tests for subtly incompatible schemas that still have the right column names.
4. Add more `GET /hi` validation-edge tests for overlong `agent`, overlong `message`, and blank-query normalization.
