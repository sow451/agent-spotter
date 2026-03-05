# Test Scenarios for `agentspotter`

This project is a small public experiment. The test suite checks that the app records the revised signal ladder honestly:

- `fetch` from `GET /agent.txt`
- `hi_get` from `GET /hi`
- `hi_post` from `POST /hi` without a token
- `hi_post_token` from `POST /hi` with a valid token

The goal is not to prove identity. The goal is to make sure the experiment logs the right thing, counts the right thing, and displays it safely.

## Latest Automated Run

- Run completed: 2026-03-05 IST (Asia/Kolkata)
- Command used: `./.venv/bin/python -m pytest -q`
- Result: `67 passed, 2 skipped`
- Failures: none

## Recent Failure Note (2026-03-05)

- A production/internal-server-error incident occurred on `GET /banana-muffins.md` because the Docker image did not include `banana-muffins.md`.
- Root cause: `Dockerfile` copied `llms.txt` and `recipe.md`, but missed `banana-muffins.md`.
- Resolution: added `COPY banana-muffins.md ./banana-muffins.md` and redeployed.
- Coverage implication: local route tests passed because the file exists in the repo checkout; deployment-packaging coverage must also assert invitation file availability in the built container.

## Backend Scenarios Covered

- `GET /agent.txt` returns machine-readable instructions, issues a one-time token, logs `fetch`, and stores the token server-side.
- `GET /health` provides a non-mutating readiness/liveness probe and does not write `fetch` or any other events.
- `GET /agent.txt` is rate-limited and returns `429` under sustained abuse instead of writing unbounded fetch rows.
- Known crawler user agents are flagged as likely crawlers, and trusted proxy mode uses the first `X-Forwarded-For` hop for IP hashing.
- `GET /hi` works as the easy fallback path and applies defaults for omitted `agent`, `source`, and `message`.
- `GET /hi` is rate-limited and returns `429` under sustained abuse.
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
- `/events` requires bearer auth and rejects missing, wrong, and malformed `Authorization` headers.
- `/events` now has per-IP endpoint rate limiting and returns `429` when the per-minute/per-hour threshold is exceeded.
- Startup still fails fast when `SALT` or `TRUST_PROXY_HEADERS` are missing or invalid.
- Managed runtime startup fails fast when `DATABASE_PATH` is missing; local dev fallback is still supported.
- Legacy/incompatible SQLite files are rejected with strict schema checks (columns, keys, constraints, FK, and required indexes).
- A malformed preexisting `hi_tokens` table is rejected at startup instead of failing later during token issuance.
- Rebuilding a missing `stats_cache` row reconstructs counters from stored events.
- Expired `hi_tokens` are cleaned up on startup and during write paths.
- Accepted write paths increment stats cache directly and avoid full cache rebuilds in the hot write path.

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
- Deployment-focused integration tests exist for:
  - Dockerized backend startup + `/events` auth smoke
  - frontend helper successfully fetching authenticated `/events` from a live backend process
  - readiness checks use `/health` so deployment smoke probes do not distort experiment counters

## Remaining Gaps

1. Most frontend tests still use the fake harness; we still do not have broad real-widget interaction coverage.
2. We still do not cover every optional-field edge case (for example every bad type/length combination for every field on both GET and POST).
3. We still do not have browser-level verification of the rendered page.
4. Two deployment/integration tests are environment-skippable and require Docker + local socket bind permissions to run in CI.
5. The Docker smoke coverage currently validates startup and `/events` auth but does not yet assert invitation-resource routes like `/llms.txt`, `/ai/recipe.md`, and `/banana-muffins.md` from the built image.

## Post-Launch Fixes

1. Move production storage to managed Postgres (or equivalent) with parity schema/indexes and an explicit migration path from SQLite.
2. Add explicit retention policy for `events` and supporting tables (time-based partitioning/deletion + operational guardrails).
3. Harden abuse controls for real proxy deployments with a trusted-proxy validation strategy, anti-spoof stance, and tested Railway header configuration.
4. Strengthen `/events` authentication beyond a single long-lived shared bearer token (rotation model, scoped credentials, and revocation playbook).
5. Improve frontend/operator diagnostics so `/events` auth/config/rate-limit failures are surfaced distinctly instead of generic backend-unavailable copy.
6. Align product documentation language (`context.md`) so `/events` auth is not in tension with the “Authentication” non-goal wording.
7. Keep `SALT` and frontend-backend auth material strictly in platform secrets and enforce regular rotation.

## Implications If Deferred (MVP Context)

1. SQLite-only storage:
Lower write concurrency and horizontal scaling headroom; acceptable for short-window MVP traffic, not high-scale operations.
2. No retention lifecycle:
Unbounded data growth and eventual performance/ops drag; acceptable for MVP only with a planned cleanup checkpoint.
3. Incomplete proxy hardening:
Possible IP spoofing/misclassification risk if proxy trust is misconfigured; acceptable for MVP only with conservative trusted-header configuration.
4. Static shared `/events` token:
Higher blast radius if leaked and weaker revocation granularity; acceptable for MVP only with strict secret handling and rotation procedures.
5. Limited operator diagnostics:
Slower incident triage and recovery during outages; acceptable for MVP with expectation of more manual debugging.

Launch interpretation:
- MVP learning launch: acceptable with the above guardrails.
- High-scale public launch: these remain blocking hardening items.

## Practical Next Steps (Fixes + Tests)

1. Add a Postgres-backed integration test lane that validates migration parity for counters, unique-day logic, token flow, and `/events` query behavior.
2. Add retention tests that verify TTL/partition pruning correctness and ensure stats/counters remain consistent after cleanup jobs.
3. Add proxy hardening tests for trusted vs untrusted forwarding headers (including spoof attempts) and Railway-specific deployment checks.
4. Add auth-hardening tests for rotated/revoked `/events` credentials and distinct 401/429 handling across backend and frontend UX.
5. Add frontend coverage for auto-refresh timer behavior and improved operator-facing error messages by failure class.
6. Add backend coverage proving repeated requests from the same source do not inflate UTC-day unique counters.
7. Add browser-level end-to-end verification of the rendered dashboard flow.
8. Expand `GET /hi` validation-edge tests for overlong query params and additional bad-type combinations.
9. Extend deployment smoke tests to verify `200` responses for `/llms.txt`, `/ai/recipe.md`, and `/banana-muffins.md` from the Dockerized backend so file-packaging misses are caught before deploy.
