# agentspotter

This `agentspotter` repo contains a small public experiment for observing whether web visitors behave like passive fetchers or interactive clients that can follow instructions and submit a valid follow-up request. 

The docs are split by purpose so it is easy to navigate: `context.md` explains why the experiment exists, `implementation.md` is the authoritative build contract, and `plan.md` is the execution plan aligned to that contract.

- `context.md` = why
- `implementation.md` = authoritative contract / how
- `plan.md` = aligned execution plan / what
- `copy.md` = website copy

## Railway backend deployment

The FastAPI backend can now be deployed to Railway directly from the repo root with the included `Dockerfile`. This avoids Railway picking up the separately deployed Streamlit frontend dependencies by mistake.

Use these settings when you create the Railway service:

- Source: this repository root
- Runtime: Dockerfile (auto-detected from the repo root)
- Exposed port: Railway injects `PORT`; the container already binds to it

Set these required environment variables in Railway before the service starts:

- `SALT`: any strong, unique secret string
- `TRUST_PROXY_HEADERS`: set to `false` by default; only set it to `true` if you have explicitly verified Railway overwrites and sanitizes `X-Forwarded-For`
- `FRONTEND_API_TOKEN`: a shared bearer token the Streamlit frontend uses when calling `/events`

Required for durable data:

- Add a Railway `Volume`
- Mount it at `/data`
- Set `DATABASE_PATH=/data/events.db`

Where these values go in Railway:

- Open the service, then `Variables`
- Add `SALT`, `TRUST_PROXY_HEADERS`, `FRONTEND_API_TOKEN`, and `DATABASE_PATH` there
- Add the volume from the service settings, then set its mount path to `/data`
- `SALT` is the actual secret here; `DATABASE_PATH` is just configuration

Operational note:

- If you do not mount a volume, SQLite writes stay inside the container and can disappear after a redeploy or container replacement.
- If you reuse an older mounted `events.db` with an incompatible schema, rotate or remove that file before deploying this version.

## Streamlit frontend deployment

Deploy the frontend separately on Streamlit Community Cloud.

Use these settings when you create the Streamlit app:

- Repository: this repository
- Main file path: `frontend/app.py`

This repo includes a root `requirements.txt` that points at `frontend/requirements.txt`, so Streamlit installs the frontend dependencies from the repo root automatically.

Where the frontend backend URL goes in Streamlit:

- Open the Streamlit app settings
- Go to `Secrets`
- Add:

```toml
BACKEND_URL = "https://your-backend.up.railway.app"
FRONTEND_API_TOKEN = "replace-with-the-same-token-you-set-on-railway"
```

The frontend now reads `BACKEND_URL` from Streamlit secrets first if it is not set as an environment variable, so the standard Streamlit secrets flow works without any extra code changes.
`BACKEND_URL` is configuration rather than a secret, but Streamlit Secrets is still the right place to store it operationally.
`FRONTEND_API_TOKEN` is the real shared secret on the Streamlit side and must exactly match the Railway value.
