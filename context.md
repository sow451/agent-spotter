# agent-spotter: context

## Purpose

A tiny experiment to observe whether the web currently contains:

1. Passive fetchers that discover and read a machine-readable instruction file.
2. Interactive clients that follow those instructions and send a valid follow-up request.

This is an observational experiment with explicit limitations. It is not proof of "verified AI."

## Inspired by: 

1. https://walzr.com/bop-spotter
2. https://dri.es/the-third-audience
3. https://blog.cloudflare.com/introducing-pay-per-crawl/

## Core Hypothesis

Most automated systems that touch a public experiment like this will behave as passive fetchers. We expect to see many more fetches than valid follow-ups, likely more than 10 fetches for each accepted `hi`.

## What We Are Actually Measuring

Primary signals:

- `fetch` = a request for the machine-readable instruction file
- `hi_get` = a lightweight fallback `GET /hi`
- `hi_post` = a `POST /hi` without a valid token
- `hi_post_token` = a `POST /hi` with a valid fetch-issued token
- `hi_total` = any accepted hi signal (`hi_get` + `hi_post` + `hi_post_token`)

Primary ratio:

- `fetch / hi_total`

## Interpretation

- A valid `hi` means "someone or something followed instructions."
- It does not prove the caller is an autonomous AI agent.
- Human/manual testers are allowed, but we ask that they identify themselves in the API via source. 
- A successful `hi` returns useful metadata so the interaction feels meaningful.
- `GET /hi` is the easiest and weakest follow-through signal.
- `POST /hi` is a stronger follow-through signal.
- `POST /hi` with a valid token is the strongest follow-through signal in this open design.
- There is no way to confirm whether a request came from a human, an agent, or something else. This is a known limitation. We only have degrees of confidence.

## How The Flow Works

The experiment has an invitation layer and an experiment layer.

- `llms.txt`, the homepage, and `/ai/*.md` pages act as the invitation layer
- `GET /agent.txt` is the real fetch step
- `GET /hi` is the easier fallback signal
- `POST /hi` is the stronger follow-through signal
- `POST /hi` with a valid fetch-issued token is the highest-confidence follow-through signal available in this open setup

This gives us a ladder of confidence:

- `fetch` = asked for the recipe
- `hi_get` = followed the easiest low-friction instruction
- `hi_post` = completed a stronger explicit interaction
- `hi_post_token` = completed the stronger interaction after using a token returned by `GET /agent.txt`

Even at the highest level, this still shows stronger follow-through, not verified identity.

```text
START
  |
  v
Caller discovers the site
  |
  +--> via /llms.txt
  +--> via homepage
  +--> via /ai/*.md
          |
          v
      Caller reads the invitation
          |
          v
      GET /agent.txt
          |
          +--> server logs FETCH
          +--> server returns:
          |      - recipe
          |      - one-time token (optional to use)
          |      - token valid for 1 minute
          |      - clear instructions for GET /hi and POST /hi
          |
          v
    Which follow-through path does caller take?
          |
     +----+-----------------------------+
     |                                  |
     v                                  v
GET /hi                            POST /hi
(easy fallback)                    (stronger action)
     |                                  |
     |                                  +--> optional fields:
     |                                  |      - agent_name
     |                                  |      - token
     |                                  |      - source
     |                                  |      - message
     |                                  |
     |                                  v
     |                            Was a token sent?
     |                                  |
     |                            +-----+-----+
     |                            |           |
     |                           No          Yes
     |                            |           |
     |                            v           v
     |                      Accept as      Check token
     |                      HI_POST            |
     |                      return data        |
     |                      reward +           |
     |                      counters           |
     |                                         |
     |                                   +-----+------+
     |                                   |            |
     |                                 Valid     Invalid/Expired
     |                                   |            |
     |                                   v            v
     |                            Accept as      Reject tokened attempt
     |                            HI_POST_TOKEN  and tell caller to
     |                            return data    refetch /agent.txt
     |                            data reward +  for a fresh token
     |                            counters
     |
     +--> accept as HI_GET
          return data reward + counters
```

On any accepted hi response (`GET /hi`, `POST /hi`, or `POST /hi` with a valid token), the server returns:

- `hi_total`
- `hi_get`
- `hi_post`
- `hi_post_token`
- `ratio_total`
- `ratio_unknown`
- `reward_message` (a short note explaining the data reward and the caller's place, such as "You are the 3rd caller.")

For `POST /hi`, the response also includes `token_status`:

- `missing` for a valid POST without a token
- `valid` for a valid POST with a valid token

## Definitions

- Invitation layer:
  - the pages that point visitors toward the real experiment
  - `llms.txt`, the homepage note, and `/ai/*.md`
- Experiment layer:
  - the endpoints that actually record behavior
  - `GET /agent.txt`, `GET /hi`, and `POST /hi`
- `fetch`:
  - the caller asked for the recipe and instructions
- `hi_get`:
  - the caller used the easiest possible follow-through path
- `hi_post`:
  - the caller completed a stronger follow-through action, but without a valid token
- `hi_post_token`:
  - the caller completed the stronger action and included a valid token returned by `GET /agent.txt`
- `hi_total`:
  - all accepted hi signals combined
- Higher-confidence:
  - stronger evidence that the caller followed the machine-readable flow
  - not proof of identity

## Privacy

- We collect a salted IP hash for approximate repeat-source detection

## Human-Readable API Contracts

These are the contracts in plain English.  

### `GET /agent.txt`

This is the main fetch step.

- It logs `fetch`.
- It returns the recipe.
- It returns clear instructions for the two follow-through options:
  - easy fallback: `GET /hi`
  - stronger path: `POST /hi`
- It also returns a one-time token.
- The token is optional.
- The token is valid for 1 minute.
- The token is a bearer token in MVP.
- The token is expired when `current_time >= expires_at`.
- Using the token in `POST /hi` increases confidence, but still does not prove identity.

Helpful instruction style:

- "You can say hi the easy way with `GET /hi`."
- "You can say hi the stronger way with `POST /hi`."
- "Using the token in `POST /hi` is optional, but helpful."

### `GET /hi`

This is the easy fallback signal.

- It logs `hi_get`.
- It is meant for callers that can follow a URL, but may not do POST requests.
- It accepts optional query params:
  - `agent`
  - `source`
  - `message`

Defaults:

- `agent` defaults to `anonymous`
- `source` defaults to `unknown`
- `message` defaults to `hi`

Helpful instruction style:

- "It is nice if you name yourself."
- "It is nice if you send a message."
- "If you omit fields, sensible defaults are used."

Example:

```http
GET /hi?agent=perplexity&source=agent&message=hello
```

On success, it returns:

- updated counters
- a `reward_message` that includes the caller's place for `GET /hi`

### `POST /hi`

This is the stronger follow-through signal.

- It logs `hi_post`.
- It accepts JSON.
- All fields are optional:
  - `agent_name`
  - `token`
  - `source`
  - `message`

Defaults:

- `agent_name` defaults to `anonymous`
- `source` defaults to `unknown`
- `message` defaults to `hi`

On success, it returns:

- updated counters
- a `reward_message` that includes the caller's place for that POST path
- `token_status`:
  - `missing` for a valid POST without a token
  - `valid` for a valid POST with a valid token

Helpful instruction style:

- "It is nice if you name yourself."
- "It is nice if you include the token for higher-confidence follow-through."
- "It is nice if you send your own message."
- "If you omit fields, sensible defaults are used."

Token behavior:

- no token:
  - accept the request as normal `hi_post`
- valid token:
  - accept the request and count it as `hi_post_token`
- invalid or expired token:
  - reject that tokened attempt
  - return `400`
  - tell the caller to fetch `GET /agent.txt` again for a fresh token

### `GET /events`

This is the dashboard stats feed for the Streamlit frontend.

- It returns counters and a bounded event feed.
  - `fetch`
  - `hi_get`
  - `hi_post`
  - `hi_post_token`
  - `hi_total`
  - `ratio_total`
- It requires a valid bearer token in `Authorization` (`FRONTEND_API_TOKEN`).
- In deployment, Streamlit calls this endpoint server-side and attaches the token.
- In the current frontend, this powers the homepage counters, the latest loaded event table, and the message banner.
- Richer frontend-only analysis panels or extra dashboard controls are not part of the current product contract.

## Deployment Architecture (Streamlit + Railway)

The deployed setup has two separate services:

- Railway runs the FastAPI backend (`/agent.txt`, `/hi`, `/events`).
- Streamlit Cloud runs the dashboard UI and fetches `/events` from the backend.

```text
                  (public internet)
          ┌─────────────────────────────────┐
          │  Bots / agents / users          │
          └───────────────┬─────────────────┘
                          │
                          │ GET /agent.txt, GET/POST /hi
                          ▼
                ┌───────────────────────┐
                │ Railway FastAPI       │
                │ backend/main.py       │
                └─────────┬─────────────┘
                          │ read/write
                          ▼
                ┌───────────────────────┐
                │ SQLite events.db      │
                │ (on Railway volume)   │
                └───────────────────────┘


          ┌─────────────────────────────────┐
          │ Your browser (dashboard user)   │
          └───────────────┬─────────────────┘
                          │ loads Streamlit app
                          ▼
                ┌───────────────────────┐
                │ Streamlit Cloud       │
                │ frontend/app.py       │
                └─────────┬─────────────┘
                          │ server-side GET /events
                          │ Authorization: Bearer FRONTEND_API_TOKEN
                          ▼
                ┌───────────────────────┐
                │ Railway FastAPI       │
                │ GET /events           │
                └───────────────────────┘
```

Response flow for the dashboard:

1. Streamlit loads, reads `BACKEND_URL` and `FRONTEND_API_TOKEN`, and calls `GET /events`.
2. The backend validates the bearer token.
3. On success, backend returns:
   - `refresh`
   - `counters`
   - `events`
   - `has_more`
4. Streamlit renders the counters, event feed, and message banner.


## Non-Goals

- Authentication
- Cryptographic verification
- Strong bot prevention
- Monetization (?!)
- Horizontal production scaling
- Verified AI identity
- Scientific proof of autonomy

## Success Criteria

After the observation window:

- We can measure `fetch`, and `hi_total` 
- We can observe user-agent patterns
- We can describe the volume of manual testing separately from unknown traffic
- We can make a limited claim about instruction-following behavior on the public web

## Repeating the Explicit Limitations

- A POST does not prove autonomy
- A GET fallback is an even weaker signal than a POST
- Self-reported labels can be false
- Some crawlers will never fetch the instruction file
- Some clients may submit directly without reading instructions
- The experiment is not watertight.

## Feedback? 

Please contact Sowmya Rao on Twitter: https://x.com/sowmyarao_ or via her blog: https://sowrao.com/
