# Hermes Agent — Railway Template

Deploy [Hermes Agent](https://github.com/NousResearch/hermes-agent) on [Railway](https://railway.app) with a web-based admin dashboard for configuration, gateway management, and user pairing.

[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/hermes-agent-ai?referralCode=QXdhdr&utm_medium=integration&utm_source=template&utm_campaign=generic)

> Hermes Agent is an autonomous AI agent by [Nous Research](https://nousresearch.com/) that lives on your server, connects to your messaging channels (Telegram, Discord, Slack, etc.), and gets more capable the longer it runs.

<!-- TODO: Add dashboard screenshot -->
<!-- ![Dashboard](docs/dashboard.png) -->

## Features

- **Admin Dashboard** — dark-themed UI to configure providers, channels, tools, and manage the gateway
- **One-Page Setup** — provider dropdown, checkbox-based channel/tool toggles — no config files to edit
- **Gateway Management** — start, stop, restart the Hermes gateway from the browser
- **Live Status** — stat cards for gateway state, uptime, model, and pending pairing requests
- **Live Logs** — streaming gateway log viewer
- **User Pairing** — approve or deny users who message your bot, revoke access anytime
- **Hermes Desktop remote** — connect your Mac app to the Railway backend on port 9119
- **Basic Auth** — password-protected admin panel at `/setup`
- **Reset Config** — one-click reset to start fresh

## Getting Started

The easiest way to get started:

### 1. Get an LLM Provider Key (free)

1. Register for free at [OpenRouter](https://openrouter.ai/)
2. Create an API key from your [OpenRouter dashboard](https://openrouter.ai/keys)
3. Pick a free model from the [model list sorted by price](https://openrouter.ai/models?order=pricing-low-to-high) (e.g. `google/gemma-3-1b-it:free`, `meta-llama/llama-3.1-8b-instruct:free`)

### 2. Set Up a Telegram Bot (fastest channel)

Hermes Agent interacts entirely through messaging channels — there is no chat UI like ChatGPT. Telegram is the quickest to set up:

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot`, follow the prompts, and copy the **Bot Token**
3. Send a message to your new bot — it will appear as a pairing request in the admin dashboard
4. To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot)

### 3. Deploy to Railway

1. Click the **Deploy on Railway** button above
2. Set the `ADMIN_PASSWORD` environment variable (or a random one will be generated and printed to deploy logs)
3. Attach a **volume** mounted at `/data` (persists config across redeploys)
4. Open **`https://your-app.up.railway.app/setup`** — log in with username `admin` and your password

### 4. Configure in the Admin Dashboard

1. **LLM Provider** — select OpenRouter from the dropdown, paste your API key, enter the model name
2. **Messaging Channel** — check Telegram, paste the Bot Token from BotFather
3. Click **Save & Start** — the gateway will start and your bot goes live

### 5. Start Chatting

Message your Telegram bot. If you're a new user, a pairing request will appear in the admin dashboard under **Users** — click **Approve**, and you're in.

<!-- TODO: Add Telegram chat screenshot -->
<!-- ![Telegram Example](docs/telegram-example.png) -->

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `9119` | Public port (Caddy). Railway sets this automatically — Hermes Desktop expects `:9119`. |
| `HERMES_SERVE_PORT` | `9120` | Internal port for `hermes serve` (Desktop + web UI backend) |
| `ADMIN_INTERNAL_PORT` | `8080` | Internal port for the admin setup UI (`/setup`, `/health`) |
| `PROXY_HOST_ROUTES` | *(unset)* | Optional comma-separated `hostname:internal_port` for API server host routing — see [API server hostnames](#api-server-hostnames-optional) |
| `ADMIN_USERNAME` | `admin` | Admin UI login username (`/setup`) |
| `ADMIN_PASSWORD` | *(auto-generated)* | Admin UI password — if unset, a random password is printed to logs |
| `HERMES_DASHBOARD_BASIC_AUTH_*` | *(unset)* | Hermes auth for Desktop + web UI — set in `/data/.hermes/.env` or Railway vars; see [Connect Hermes Desktop](#connect-hermes-desktop-macos) |
| `HERMES_DASHBOARD_PUBLIC_URL` | *(unset)* | Public URL of your Railway app — required for OAuth callbacks (e.g. `https://your-app.up.railway.app`) |
| `HERMES_REF` | *(pinned in Dockerfile)* | Hermes Agent version to install (any upstream git tag/branch). Set this to override the Dockerfile default without editing code — see [Updating Hermes](#updating-hermes). |
| `MIDAS_GITHUB_TOKEN` | *(unset)* | Build-time only: fine-grained GitHub PAT with read-only **Contents** access to `midianio/midas`, used to download the `midas` release binary into the image. If unset, the midas install is skipped and the image still builds. |
| `MIDAS_VERSION` | `latest` | midas release to bake into the image. Defaults to the newest GitHub release at build time; set a release tag (e.g. `0.1.2`) to pin. |
| `OBSIDIAN_HEADLESS_VERSION` | *(pinned in Dockerfile)* | [`obsidian-headless`](https://obsidian.md/help/publish/headless) npm version (`ob` CLI for Obsidian Sync/Publish without the desktop app). |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

### Baked-in agent CLIs

The image ships with `midas` (Midian's CLI) and `ob` (Obsidian headless Sync/Publish) so the agent can use them without any per-restart install. `ob` needs a one-time login after first deploy — credentials land under `$HOME` (`/data`, the persistent volume) and survive restarts/redeploys:

```
ob login
ob publish-list-sites
cd <vault> && ob publish-setup --site "<site>"
ob publish            # or: ob publish --dry-run
```

Requires an active Obsidian Publish (and/or Sync) subscription.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container ($PORT, default 9119)
├── Caddy (edge router on $PORT)
│   ├── /setup, /health, /login  →  Admin server (:8080)
│   └── everything else            →  hermes serve (:9120) — Desktop + web UI
├── Python Admin Server (Starlette + Uvicorn, loopback :8080)
│   ├── /setup      — Setup wizard (admin cookie auth)
│   ├── /health     — Health check (no auth)
│   └── /setup/api/* — Config, status, logs, gateway, pairing
├── hermes serve    — Headless backend for Hermes Desktop + browser chat
└── hermes gateway  — Messaging channels (Telegram, Discord, …)
```

Caddy owns Railway's public port. The admin UI lives at **`/setup`** (log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD`). The root URL serves **`hermes serve`**, which uses separate Hermes auth for Desktop and browser clients.

Config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Connect Hermes Desktop (macOS)

1. **Configure Hermes auth** in `/data/.hermes/.env` (via the admin UI env editor, or Railway variables):

   **Public Railway URL (recommended — OAuth):**

   ```bash
   HERMES_DASHBOARD_OAUTH_CLIENT_ID=agent:...
   HERMES_DASHBOARD_PUBLIC_URL=https://your-app.up.railway.app
   ```

   Register the OAuth client with `hermes dashboard register` from a one-off `docker exec` or local Hermes install pointing at the same `HERMES_HOME`.

   **Tailscale / VPN only (basic auth):**

   ```bash
   HERMES_DASHBOARD_BASIC_AUTH_USERNAME=admin
   HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=<strong-password>
   HERMES_DASHBOARD_BASIC_AUTH_SECRET=<openssl rand -base64 32>
   ```

2. **In Hermes Desktop** → Settings → Gateway → **Remote gateway**:
   - Remote URL: `https://your-app.up.railway.app` (port 9119 is the default `$PORT`; Railway's HTTPS URL works without `:9119`)
   - Sign in (OAuth or the basic-auth credentials above)
   - Save and reconnect

3. **Verify** from your Mac:

   ```bash
   curl -s https://your-app.up.railway.app/api/status | jq '.auth_required, .auth_providers'
   ```

   You should see `"auth_required": true` and your provider listed.

Admin login (`/setup`) and Hermes serve auth are **separate**. `ADMIN_PASSWORD` protects the setup wizard only; Desktop uses `HERMES_DASHBOARD_*` credentials.

## API server hostnames (optional)

Railway exposes **one HTTP port** per service. To reach Hermes API servers on **8642** / **8643** via different hostnames (Open WebUI, gateway proxy mode, etc.), use `PROXY_HOST_ROUTES`.

### 1. Enable the API server in `/data/.hermes/.env`

```bash
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642          # use 8643 in a second profile's .env
API_SERVER_KEY=<random-secret> # openssl rand -hex 32
```

### 2. Route hostnames via Caddy

```bash
PROXY_HOST_ROUTES=api-one.example.com:8642,api-two.example.com:8643
```

| Host header | Internal backend |
|-------------|------------------|
| `api-one.example.com` | `127.0.0.1:8642` |
| `api-two.example.com` | `127.0.0.1:8643` |
| default Railway URL | `hermes serve` (Desktop / web UI) |
| `/setup` on any host | admin UI |

Add custom domains in Railway **Settings → Networking → Custom Domain** and point DNS at Railway's target.

### Local smoke test

```bash
docker build -t hermes-agent .
docker run --rm -p 9119:9119 \
  -e PORT=9119 \
  -e ADMIN_PASSWORD=changeme \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=hermes \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=changeme \
  -v hermes-data:/data \
  hermes-agent
```

- Admin setup: `http://localhost:9119/setup` (admin / changeme)
- Serve status: `curl http://localhost:9119/api/status`
- Desktop remote URL: `http://localhost:9119` (sign in as hermes / changeme)

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 9119:9119 \
  -e PORT=9119 \
  -e ADMIN_PASSWORD=changeme \
  -e HERMES_DASHBOARD_BASIC_AUTH_USERNAME=hermes \
  -e HERMES_DASHBOARD_BASIC_AUTH_PASSWORD=changeme \
  -v hermes-data:/data \
  hermes-agent
```

- Admin setup: `http://localhost:9119/setup` — log in with `admin` / `changeme`
- Hermes serve (Desktop): `http://localhost:9119` — sign in with `hermes` / `changeme`

## Updating Hermes

This template pins a specific Hermes Agent release in the `Dockerfile` (`ARG HERMES_REF`, currently `v2026.7.1`). To upgrade:

- **Recommended:** set a `HERMES_REF` service variable in Railway to any upstream [release tag](https://github.com/NousResearch/hermes-agent/releases) (e.g. `v2026.7.1`), then redeploy. It's passed in as a Docker build arg and overrides the Dockerfile default — no code change needed.
- **Or** bump `ARG HERMES_REF` in the `Dockerfile` and redeploy.

The "Update" button inside the Hermes dashboard is a **no-op on Railway** (it detects a container install and refuses) — the image is immutable, so a runtime self-update wouldn't survive a redeploy. Bump `HERMES_REF` and redeploy instead. When jumping releases, re-check that the Dockerfile's install extras still match upstream's `pyproject.toml`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
