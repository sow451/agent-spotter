# agentspotter Build Plan

This plan is the high-level build spec for the revised experiment. If there is any ambiguity here, `implementation.md` is the source of truth for exact API behavior, validation, storage rules, and edge cases while the codebase catches up.

## Goal

Ship an MVP with:

- FastAPI backend
- Streamlit frontend
- SQLite persistence
- basic abuse hardening for a public experiment

The MVP measures instruction-following behavior.
It does not verify AI identity.

## Core Product Model

The product now has two layers:

- Invitation layer:
  - `/llms.txt`
  - homepage note for agents
  - `/ai/*.md` pointer pages
- Experiment layer:
  - `GET /agent.txt`
  - `GET /hi`
  - `POST /hi`

The invitation layer points callers toward the experiment.
The experiment layer records what they actually do.
The invitation pages may live on a different service, but they must always point callers to the canonical backend origin for `/agent.txt`, `/hi`, and `/events`.

## Signal Ladder

The MVP should track four distinct signals:

- `fetch` = caller requested `GET /agent.txt`
- `hi_get` = caller used the easy fallback `GET /hi`
- `hi_post` = caller used `POST /hi` without a valid token
- `hi_post_token` = caller used `POST /hi` with a valid token issued by `GET /agent.txt`

Derived aggregate:

- `hi_total` = `hi_get + hi_post + hi_post_token`

Primary ratio:

- `hi_total / fetch`

Secondary ratio:

- `hi_unknown / fetch`

Interpretation:

- `hi_get` is the weakest follow-through signal
- `hi_post` is a stronger signal
- `hi_post_token` is the strongest follow-through signal in this open design
- none of these prove identity

## Repo Structure

```text
agentspotter/
  backend/
    main.py
    db.py
    requirements.txt
  frontend/
    app.py
    requirements.txt
  README.md
```

## Step 1 - Backend (FastAPI)

### Dependencies

- `fastapi`
- `uvicorn`
- `pydantic`
- `sqlite3` (built-in)
- `hashlib` (built-in)
- `secrets` (built-in)

### Storage

Use SQLite for MVP.

At minimum, backend storage must support:

- raw event logging
- short-lived token issuance and validation
- cached counters for `/events`
- approximate UTC-day unique-source counting using a salted `ip_hash`

### High-Level Tables

- `events`
  - stores fetches and hi events
- `hi_tokens`
  - stores one-time tokens issued by `GET /agent.txt`
- `source_windows`
  - stores approximate unique-source day buckets
- `stats_cache`
  - stores precomputed counters for `GET /events`

### Event Model

The backend should log these event types:

- `fetch`
- `hi_get`
- `hi_post`

Token-validated `POST /hi` remains `event_type = hi_post`, but it must also set a boolean flag showing that the request used a valid token.

### Token Policy

- `GET /agent.txt` returns one token
- the token is a bearer token in MVP
- the token is optional to use
- token lifetime is 1 minute
- token is single-use
- token is never re-issued by `GET /hi` or `POST /hi`
- a token is expired when `current_time >= expires_at`
- if a token is invalid or expired, return `400` and tell the caller to fetch `GET /agent.txt` again for a fresh token

### Abuse Controls

Keep basic rate limiting in MVP:

- rate limit `POST /hi` by `ip_hash`
- suggested starting point:
  - 3 successful `POST /hi` writes per minute
  - 20 successful `POST /hi` writes per hour
- over-limit requests return `429`

### IP Hashing

- store only a salted IP hash
- do not store raw IP
- use the hash for approximate repeat-source detection and basic rate limiting

## Step 2 - API Surface

### `GET /agent.txt`

Purpose:

- logs `fetch`
- returns the recipe
- returns clear instructions for both follow-through paths
- returns a one-time token valid for 1 minute

The instructions should say clearly:

- `GET /hi` is the easy fallback
- `POST /hi` is the stronger path
- using the token in `POST /hi` increases confidence
- later fields are optional and sensible defaults are used

### `GET /hi`

Purpose:

- easy fallback signal for callers that can follow a URL but may not do POST requests

Behavior:

- logs `hi_get`
- accepts optional query params:
  - `agent`
  - `source`
  - `message`
- returns status, counters, and a small reward message

### `POST /hi`

Purpose:

- stronger follow-through signal

Behavior:

- logs `hi_post`
- accepts JSON
- all fields are optional:
  - `agent_name`
  - `token`
  - `source`
  - `message`

Defaults:

- `agent_name` defaults to `anonymous`
- `source` defaults to `unknown`
- `message` defaults to `hi`

Helpful caller guidance:

- it is nice if callers identify themselves
- it is nice if callers include the token for higher-confidence follow-through
- it is nice if callers send their own message

Contract:

- if token is missing, accept the request normally
- if token is present and valid, accept the request and mark it as high-confidence
- if token is present but invalid or expired, reject that attempt with `400` and tell the caller to refetch `GET /agent.txt`

### `GET /events`

Purpose:

- returns counters and a bounded event feed
- also returns refresh metadata and a `has_more` flag for pagination/countdown behavior

The response should make these counters explicit:

- `fetch`
- `hi_get`
- `hi_post`
- `hi_post_token`
- `hi_total`
- `hi_unknown`
- `hi_manual`
- `hi_agent`
- approximate UTC-day unique counters
- `ratio_total`
- `ratio_unknown`
- `refresh` metadata (`cadence_seconds`, `cadence_minutes`, `last_refreshed_at`, `next_refresh_at`)
- `has_more` for older-page availability

## Step 3 - Frontend (Streamlit)

### Layout

Two columns.

Left:

- concise explanation of the experiment
- simple note for agents and humans

Right:

- counters
- filters
- event feed

### UI Behavior

- single-page dashboard
- show the signal ladder clearly
- distinguish `hi_get`, `hi_post`, and `hi_post_token`
- label token-backed signals as higher-confidence, not verified
- render all user-provided text as plain text only

### Frontend Contract

The frontend should treat `implementation.md` as the source of truth for:

- exact counter names
- exact event row shape
- exact filter behavior
- the canonical backend origin for API calls

## Step 4 - Deployment

Option A: Render

- backend service
- frontend service
- set `BACKEND_URL` in frontend
- set `SALT` in backend
- set `TRUST_PROXY_HEADERS` in backend (`true` only behind a trusted reverse proxy; otherwise `false`)
- optionally set `DATABASE_PATH` in backend (default `events.db`)

SQLite note:

- acceptable for MVP
- not durable across service replacement unless disk is persisted

## Step 5 - Validation and Readout

After deployment:

- confirm `GET /agent.txt` works and logs `fetch`
- confirm `GET /hi` works and logs `hi_get`
- confirm `POST /hi` works without a token and logs `hi_post`
- confirm `POST /hi` works with a valid token and increments the token-backed counter
- confirm invalid or expired token attempts fail with `400` and the correct message
- confirm `/events` returns bounded responses

After the observation window:

- report `fetch`
- report `hi_get`, `hi_post`, and `hi_post_token`
- report `hi_total`
- report `ratio_total`
- report `ratio_unknown`
- report approximate unique activity
- summarize user-agent patterns
- describe limitations clearly

## Optional Enhancements (Later)

- true rolling-window unique counting
- charts
- deeper user-agent summaries
- managed Postgres / Supabase migration
