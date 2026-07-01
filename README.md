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
- **Basic Auth** — password-protected admin panel
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
4. Open your app URL — log in with username `admin` and your password

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
| `PORT` | `8080` | Web server port (set automatically by Railway) |
| `PROXY_HOST_ROUTES` | *(unset)* | Comma-separated `hostname:internal_port` pairs for Caddy host routing — see [Multiple domains on one port](#multiple-domains-on-one-port) |
| `ADMIN_INTERNAL_PORT` | `8080` | Port for the admin server when `PROXY_HOST_ROUTES` is set (Caddy owns `$PORT`) |
| `ADMIN_USERNAME` | `admin` | Basic auth username |
| `ADMIN_PASSWORD` | *(auto-generated)* | Basic auth password — if unset, a random password is printed to logs |
| `HERMES_REF` | *(pinned in Dockerfile)* | Hermes Agent version to install (any upstream git tag/branch). Set this to override the Dockerfile default without editing code — see [Updating Hermes](#updating-hermes). |

All other configuration (LLM provider, model, channels, tools) is managed through the admin dashboard.

## Supported Providers

OpenRouter, DeepSeek, DashScope, GLM / Z.AI, Kimi, MiniMax, HuggingFace

## Supported Channels

Telegram, Discord, Slack, WhatsApp, Email, Mattermost, Matrix

## Supported Tool Integrations

Parallel (search), Firecrawl (scraping), Tavily (search), FAL (image gen), Browserbase, GitHub, OpenAI Voice (Whisper/TTS), Honcho (memory)

## Architecture

```
Railway Container
├── Python Admin Server (Starlette + Uvicorn)
│   ├── /            — Admin dashboard (Basic Auth)
│   ├── /health      — Health check (no auth)
│   └── /api/*       — Config, status, logs, gateway, pairing
└── hermes gateway   — Managed as async subprocess
```

The admin server runs on `$PORT` and manages the Hermes gateway as a child process. Config is stored in `/data/.hermes/.env` and `/data/.hermes/config.yaml`. Gateway stdout/stderr is captured into a ring buffer and streamed to the Logs panel.

## Multiple domains on one port

Railway exposes **one HTTP port** per service (`$PORT`). To reach two internal apps (for example Hermes API servers on **8642** and **8643**) via different hostnames, enable the built-in **Caddy** edge proxy.

### 1. Make each Hermes API server reachable inside the container

In `/data/.hermes/.env` (or per-profile `.env` if you run multiple gateways), each API server must listen on all interfaces — not loopback:

```bash
API_SERVER_ENABLED=true
API_SERVER_HOST=0.0.0.0
API_SERVER_PORT=8642          # use 8643 in the second profile's .env
API_SERVER_KEY=<random-secret> # openssl rand -hex 32
```

Without `API_SERVER_HOST=0.0.0.0`, Caddy cannot forward traffic to the gateway even though the port is open on localhost.

### 2. Tell the container how to route hostnames

Set `PROXY_HOST_ROUTES` on the Railway service (comma-separated `host:port`):

```bash
PROXY_HOST_ROUTES=api-one.example.com:8642,api-two.example.com:8643
```

On boot, Caddy listens on Railway's `$PORT` and forwards:

| Host header | Internal backend |
|-------------|------------------|
| `api-one.example.com` | `127.0.0.1:8642` |
| `api-two.example.com` | `127.0.0.1:8643` |
| anything else (including the default `*.up.railway.app` URL) | admin UI on `:8080` |

`ADMIN_INTERNAL_PORT` defaults to `8080`; change it only if that port conflicts with another process.

### 3. Add custom domains in Railway

For each public hostname:

1. Open **Service → Settings → Networking → Custom Domain**
2. Add the domain (e.g. `api-one.example.com`, `api-two.example.com`)
3. Point your DNS CNAME at Railway's target

Railway terminates TLS at the edge; Caddy inside the container speaks plain HTTP on `$PORT`.

Keep the default Railway domain on the service for the **admin dashboard** (`/setup`, `/health`). Point custom domains only at the API hostnames you listed in `PROXY_HOST_ROUTES`.

### Local smoke test

```bash
docker build -t hermes-agent .
docker run --rm -p 8080:8080 \
  -e PORT=8080 \
  -e PROXY_HOST_ROUTES='api.localtest.me:8642,api2.localtest.me:8643' \
  -e ADMIN_PASSWORD=changeme \
  -v hermes-data:/data \
  hermes-agent
```

`localtest.me` resolves to `127.0.0.1`, so you can curl `http://api.localtest.me:8080/health` (gateway) vs `http://127.0.0.1:8080/health` (admin) after enabling the API server in `.env`.

## Running Locally

```bash
docker build -t hermes-agent .
docker run --rm -it -p 8080:8080 -e PORT=8080 -e ADMIN_PASSWORD=changeme -v hermes-data:/data hermes-agent
```

Open `http://localhost:8080` and log in with `admin` / `changeme`.

## Updating Hermes

This template pins a specific Hermes Agent release in the `Dockerfile` (`ARG HERMES_REF`, currently `v2026.6.19`). To upgrade:

- **Recommended:** set a `HERMES_REF` service variable in Railway to any upstream [release tag](https://github.com/NousResearch/hermes-agent/releases) (e.g. `v2026.6.19`), then redeploy. It's passed in as a Docker build arg and overrides the Dockerfile default — no code change needed.
- **Or** bump `ARG HERMES_REF` in the `Dockerfile` and redeploy.

The "Update" button inside the Hermes dashboard is a **no-op on Railway** (it detects a container install and refuses) — the image is immutable, so a runtime self-update wouldn't survive a redeploy. Bump `HERMES_REF` and redeploy instead. When jumping releases, re-check that the Dockerfile's install extras still match upstream's `pyproject.toml`.

## Credits

- [Hermes Agent](https://github.com/NousResearch/hermes-agent) by [Nous Research](https://nousresearch.com/)
- UI inspired by [OpenClaw](https://github.com/praveen-ks-2001/openclaw-railway) admin template
