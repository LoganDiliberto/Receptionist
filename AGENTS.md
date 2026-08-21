# AGENTS.md

## Cursor Cloud specific instructions

Funkle is an AI voice receptionist for a hair salon. One repo, two pieces:

- Backend (repo root): Python 3 / FastAPI voice bot + REST API, SQLite store. Entry: `server.py`.
- Admin UI (`admin-ui/`): Angular single-page app, served by the backend at `/admin`.

The startup update script already creates the Python venv at `.venv`, installs
`requirements.txt` into it, and runs `npm ci` in `admin-ui/`. The following are
the non-obvious things you still need to know.

### Node version (important gotcha)

Angular CLI requires Node >= 22.22.3. The default `node` on `PATH`
(`/exec-daemon/node`, v22.14.0) and nvm's pre-cached v22.22.2 are BOTH too old,
and `/exec-daemon` is force-prepended to `PATH` on every process, so `nvm use`
does NOT stick. Node 24 is installed via nvm. For ANY Angular build/serve, run it
through nvm exec so it picks up Node 24, e.g.:

- Build (backend then serves `/admin`): `cd admin-ui && nvm exec 24 npm run build`
- Live-reload dev server on :4200: `cd admin-ui && nvm exec 24 npm start`

`npm ci` and all Python/pip commands work with the default toolchain and do NOT
need `nvm exec`.

### Running the backend

- Run in dev with the venv: `.venv/bin/python server.py` (honors `.env`; defaults `HOST=0.0.0.0`, `PORT=7860`).
- A committed `.env` (external API keys blank) is enough for the admin UI, REST API (`/api/*`), and the browser WebRTC test page (`/`). It boots without any cloud keys.
- The backend only serves `/admin` after the Angular UI is built to `admin-ui/dist/admin-ui/browser/`. Without a build, `/admin` returns HTTP 503 (the REST API still works).
- Live voice conversations additionally need `OPENAI_API_KEY` + `DEEPGRAM_API_KEY`; real phone calls / SMS reminders also need Twilio creds + a public tunnel (ngrok). These are optional for admin/REST development.
- With `ADMIN_PASSWORD` unset, `/admin` and `/api` are intentionally open (no Basic Auth) — fine for local dev.

### Database (SQLite)

`receptionist.db` at the repo root (gitignored). `entrypoint.sh` handles this in
the container but is NOT used in local dev, so do it manually on a fresh DB:

- Migrate: `.venv/bin/python -m alembic upgrade head`
- Seed from the bundled workbook: `.venv/bin/python -m import_xlsx ReceptionistData.xlsx --wipe`

### Lint / test / build

There is no test suite and no configured linter. The CI gate (`.github/workflows/ci.yml`) is just:

- Python: `.venv/bin/python -m compileall -q .` (a syntax/compile check; CI deliberately avoids importing modules, which touch `.env` / external services at import time)
- Admin UI: `cd admin-ui && nvm exec 24 npm run build`
