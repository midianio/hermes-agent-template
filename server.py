"""
Hermes Agent — Railway admin server.

Responsibilities:
  - Admin UI / setup wizard at /setup (Starlette + Jinja, cookie-auth guarded)
  - Management API at /setup/api/* (config, status, logs, gateway, pairing)
  - Managed subprocesses: `hermes gateway` (messaging agent) and `hermes serve`
    (headless backend for Hermes Desktop + browser dashboard on HERMES_SERVE_PORT)
  - Cookie-based session auth at /login for the admin UI (HMAC-signed, 7-day expiry)

Public traffic on Railway's $PORT is handled by Caddy (see start.sh):
  /setup, /health, /login → this admin server (ADMIN_INTERNAL_PORT)
  everything else         → `hermes serve` (HERMES_SERVE_PORT) with Hermes auth

Configure HERMES_DASHBOARD_BASIC_AUTH_* or OAuth in /data/.hermes/.env so Desktop
and browser clients can sign in to the serve backend. Set HERMES_DASHBOARD_PUBLIC_URL
to your Railway URL when using OAuth behind Caddy.
"""

# PEP 563 lazy annotations: keeps function/parameter type hints as strings so
# they're never evaluated at import. Avoids the startup DeprecationWarning from
# annotating against websockets.WebSocketClientProtocol (renamed in websockets
# >= 14), and is forward-compatible regardless of the installed websockets
# version. Safe here — nothing in this module introspects annotations at runtime.
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.templating import Jinja2Templates

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE = Path(HERMES_HOME) / ".env"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
PAIRING_TTL = 3600

# Headless Hermes backend — Caddy on $PORT forwards public traffic here.
# Binds 0.0.0.0 so Hermes's dashboard auth gate engages (required for Desktop).
HERMES_SERVE_HOST = os.environ.get("HERMES_SERVE_HOST", "0.0.0.0")
HERMES_SERVE_PORT = int(os.environ.get("HERMES_SERVE_PORT", "9120"))
HERMES_SERVE_URL = f"http://{HERMES_SERVE_HOST}:{HERMES_SERVE_PORT}"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[server] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)
else:
    print(f"[server] Admin username: {ADMIN_USERNAME}", flush=True)

# ── Env var registry ──────────────────────────────────────────────────────────
# (key, label, category, is_secret)
ENV_VARS = [
    ("LLM_MODEL",               "Model",                    "model",     False),
    ("OPENROUTER_API_KEY",       "OpenRouter",               "provider",  True),
    ("DEEPSEEK_API_KEY",         "DeepSeek",                 "provider",  True),
    ("DASHSCOPE_API_KEY",        "Qwen Cloud (DashScope)",   "provider",  True),
    ("GLM_API_KEY",              "GLM / Z.AI",               "provider",  True),
    ("KIMI_API_KEY",             "Kimi",                     "provider",  True),
    ("MINIMAX_API_KEY",          "MiniMax",                  "provider",  True),
    ("HF_TOKEN",                 "Hugging Face",             "provider",  True),
    # Added in v2026.4.23+ (hermes v0.11.0+). All plain API-key auth — hermes
    # auto-routes by env-var presence, no extra config needed on our side.
    # OAuth-based providers (xAI Grok SuperGrok, Gemini CLI, Qwen OAuth, Claude Code)
    # are set up via the dashboard's Keys tab or HERMES_AUTH_JSON_BOOTSTRAP.
    ("NVIDIA_API_KEY",           "NVIDIA NIM",               "provider",  True),
    ("ARCEEAI_API_KEY",          "Arcee AI",                 "provider",  True),
    ("STEPFUN_API_KEY",          "Step Plan",                "provider",  True),
    ("GEMINI_API_KEY",           "Google AI Studio",         "provider",  True),
    ("NOVITA_API_KEY",           "NovitaAI",                 "provider",  True),
    ("FIREWORKS_API_KEY",        "Fireworks AI",             "provider",  True),
    ("ANTHROPIC_API_KEY",        "Anthropic (Claude)",       "provider",  True),
    ("XAI_API_KEY",              "xAI",                      "provider",  True),
    ("AWS_ACCESS_KEY_ID",        "AWS Access Key ID",        "provider",  True),
    ("AWS_SECRET_ACCESS_KEY",    "AWS Secret Access Key",    "bedrock",   True),
    ("AWS_DEFAULT_REGION",       "AWS Region",               "bedrock",   False),
    ("COPILOT_GITHUB_TOKEN",     "GitHub Copilot",           "provider",  True),
    ("GMI_API_KEY",              "GMI Cloud",                "provider",  True),
    ("OPENCODE_ZEN_API_KEY",     "OpenCode Zen",             "provider",  True),
    ("OPENCODE_GO_API_KEY",      "OpenCode Go",              "provider",  True),
    ("KILOCODE_API_KEY",         "Kilo Code",                "provider",  True),
    ("OLLAMA_API_KEY",           "Ollama Cloud",             "provider",  True),
    ("AZURE_FOUNDRY_API_KEY",    "Azure Foundry key",        "provider",  True),
    ("AZURE_FOUNDRY_BASE_URL",   "Azure Foundry URL",        "azure",     False),
    # Custom OpenAI-compatible endpoint — one slot; more via Hermes dashboard.
    # Only the API key is in category "provider" so PROVIDER_KEYS / is_config_complete
    # only trigger when an actual key is present, not just a base URL.
    ("CUSTOM_PROVIDER_API_KEY",  "Custom Provider key",      "provider",  True),
    ("CUSTOM_PROVIDER_BASE_URL", "Custom Provider base URL", "custom",    False),
    ("CUSTOM_PROVIDER_NAME",     "Custom Provider name",     "custom",    False),
    ("PARALLEL_API_KEY",         "Parallel (search)",        "tool",      True),
    ("FIRECRAWL_API_KEY",        "Firecrawl (scrape)",       "tool",      True),
    ("TAVILY_API_KEY",           "Tavily (search)",          "tool",      True),
    ("FAL_KEY",                  "FAL (image gen)",          "tool",      True),
    ("BROWSERBASE_API_KEY",      "Browserbase key",          "tool",      True),
    ("BROWSERBASE_PROJECT_ID",   "Browserbase project",      "tool",      False),
    ("GITHUB_TOKEN",             "GitHub token",             "tool",      True),
    ("VOICE_TOOLS_OPENAI_KEY",   "OpenAI (voice/TTS)",       "tool",      True),
    ("HONCHO_API_KEY",           "Honcho (memory)",          "tool",      True),
    ("TELEGRAM_BOT_TOKEN",       "Bot Token",                "telegram",  True),
    ("TELEGRAM_ALLOWED_USERS",   "Allowed User IDs",         "telegram",  False),
    ("DISCORD_BOT_TOKEN",        "Bot Token",                "discord",   True),
    ("DISCORD_ALLOWED_USERS",    "Allowed User IDs",         "discord",   False),
    ("SLACK_BOT_TOKEN",          "Bot Token (xoxb-...)",     "slack",     True),
    ("SLACK_APP_TOKEN",          "App Token (xapp-...)",     "slack",     True),
    ("WHATSAPP_ENABLED",         "Enable WhatsApp",          "whatsapp",  False),
    ("EMAIL_ADDRESS",            "Email Address",            "email",     False),
    ("EMAIL_PASSWORD",           "Email Password",           "email",     True),
    ("EMAIL_IMAP_HOST",          "IMAP Host",                "email",     False),
    ("EMAIL_SMTP_HOST",          "SMTP Host",                "email",     False),
    ("MATTERMOST_URL",           "Server URL",               "mattermost",False),
    ("MATTERMOST_TOKEN",         "Bot Token",                "mattermost",True),
    ("MATRIX_HOMESERVER",        "Homeserver URL",           "matrix",    False),
    ("MATRIX_ACCESS_TOKEN",      "Access Token",             "matrix",    True),
    ("MATRIX_USER_ID",           "User ID",                  "matrix",    False),
    ("GATEWAY_ALLOW_ALL_USERS",  "Allow all users",          "gateway",   False),
    ("ADMIN_USERNAME",           "Admin username",           "admin",     False),
    ("ADMIN_PASSWORD",           "Admin password",           "admin",     True),
]

SECRET_KEYS  = {k for k, _, _, s in ENV_VARS if s}
PROVIDER_KEYS = [k for k, _, c, _ in ENV_VARS if c == "provider"]
CHANNEL_MAP  = {
    "Telegram":    "TELEGRAM_BOT_TOKEN",
    "Discord":     "DISCORD_BOT_TOKEN",
    "Slack":       "SLACK_BOT_TOKEN",
    "WhatsApp":    "WHATSAPP_ENABLED",
    "Email":       "EMAIL_ADDRESS",
    "Mattermost":  "MATTERMOST_TOKEN",
    "Matrix":      "MATRIX_ACCESS_TOKEN",
}


# ── .env helpers ──────────────────────────────────────────────────────────────
def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_config_yaml(data: dict[str, str]) -> None:
    """Write config.yaml — deep-merge template defaults with any existing user/cron-managed sections.

    Previously this overwrote ``$HERMES_HOME/config.yaml`` with a hardcoded template
    body on every boot, silently erasing user-managed top-level keys. The most
    common casualty is ``mcp_servers`` — Hermes reads downstream MCP servers
    *only* from this file (see ``hermes_cli/mcp_config.py:_get_mcp_servers``), so
    the wipe broke ``hermes mcp add/test/list`` state across every container
    restart and required hand-restoration after each redeploy.

    The fix: load the existing file if any, apply the deployment-managed keys
    (``model.default``, ``model.provider``, ``terminal``, ``agent``, ``data_dir``)
    on top, and write the merged result. Unknown top-level keys (``mcp_servers``,
    custom skill config, etc.) are preserved verbatim.
    """
    import yaml  # hermes-agent already pulls pyyaml; deferred import keeps cold start light

    model = data.get("LLM_MODEL", "")
    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if config_path.exists():
        try:
            with config_path.open() as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (yaml.YAMLError, OSError):
            # Treat unparseable as absent — we'll overwrite with template defaults.
            existing = {}

    merged = dict(existing)

    # Deployment-managed (always authoritative — these reflect the runtime env).
    merged_model = dict(merged.get("model") if isinstance(merged.get("model"), dict) else {})
    merged_model["default"] = model
    # Only force provider="auto" when a known API key is configured. If no
    # API key is set, the user likely configured an OAuth provider (xai-oauth,
    # qwen-oauth, etc.) via the dashboard's model picker — preserve that value
    # so a container restart doesn't revert it to "auto" and break their session.
    if any(data.get(k) for k in PROVIDER_KEYS):
        merged_model["provider"] = "auto"
    merged["model"] = merged_model

    merged_terminal = dict(merged.get("terminal") if isinstance(merged.get("terminal"), dict) else {})
    merged_terminal["backend"] = "local"
    merged_terminal["timeout"] = 60
    merged_terminal["cwd"] = "/tmp"
    merged["terminal"] = merged_terminal

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent

    merged["data_dir"] = HERMES_HOME

    # Custom OpenAI-compatible endpoint — write custom_providers block when configured,
    # remove it when not (safe on Railway where users don't hand-edit config.yaml).
    custom_base_url = data.get("CUSTOM_PROVIDER_BASE_URL", "").strip()
    if custom_base_url:
        raw_name = data.get("CUSTOM_PROVIDER_NAME", "").strip() or custom_base_url
        # Sanitise to a valid hermes provider name (lowercase alphanumeric + hyphens).
        sanitized_name = re.sub(r"[^a-z0-9-]", "-", raw_name.lower()).strip("-") or "custom"
        merged["custom_providers"] = [{
            "name": sanitized_name,
            "base_url": custom_base_url,
            "key_env": "CUSTOM_PROVIDER_API_KEY",
        }]
    else:
        merged.pop("custom_providers", None)

    with config_path.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)


def write_env(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cat_order = ["model", "provider", "bedrock", "azure", "custom", "tool",
                 "telegram", "discord", "slack", "whatsapp",
                 "email", "mattermost", "matrix", "gateway", "admin"]
    cat_labels = {
        "model": "Model", "provider": "Providers",
        "bedrock": "AWS Bedrock", "azure": "Azure Foundry",
        "custom": "Custom Endpoint", "tool": "Tools",
        "telegram": "Telegram", "discord": "Discord", "slack": "Slack",
        "whatsapp": "WhatsApp", "email": "Email",
        "mattermost": "Mattermost", "matrix": "Matrix", "gateway": "Gateway",
        "admin": "Admin",
    }
    key_cat = {k: c for k, _, c, _ in ENV_VARS}
    grouped: dict[str, list[str]] = {c: [] for c in cat_order}
    grouped["other"] = []

    for k, v in data.items():
        if not v:
            continue
        cat = key_cat.get(k, "other")
        grouped.setdefault(cat, []).append(f"{k}={v}")

    lines: list[str] = []
    for cat in cat_order:
        entries = sorted(grouped.get(cat, []))
        if entries:
            lines.append(f"# {cat_labels.get(cat, cat)}")
            lines.extend(entries)
            lines.append("")
    if grouped["other"]:
        lines.append("# Other")
        lines.extend(sorted(grouped["other"]))
        lines.append("")

    path.write_text("\n".join(lines))


# ── xAI Grok SuperGrok OAuth (Device Code — RFC 8628) ───────────────────────
# xAI's OIDC discovery at https://auth.x.ai/.well-known/openid-configuration
# declares device_authorization_endpoint, so Device Code flow works without
# any redirect URL. The client_id matches hermes's own Grok CLI credential.
_XAI_CLIENT_ID   = "b1a00492-073a-47ea-816f-4c329264a828"
_XAI_SCOPE       = "openid profile email offline_access grok-cli:access api:access"
_XAI_DEVICE_URL  = "https://auth.x.ai/oauth2/device/code"
_XAI_TOKEN_URL   = "https://auth.x.ai/oauth2/token"
_XAI_GRANT_TYPE  = "urn:ietf:params:oauth:grant-type:device_code"

_xai_oauth_state: dict | None = None  # one auth at a time (single-user deployment)


def _has_xai_oauth_tokens() -> bool:
    """True when auth.json contains a valid xAI OAuth refresh token."""
    auth_path = Path(HERMES_HOME) / "auth.json"
    if not auth_path.exists():
        return False
    try:
        data = json.loads(auth_path.read_text())
        tokens = data.get("providers", {}).get("xai-oauth", {}).get("tokens", {})
        return bool(isinstance(tokens, dict) and tokens.get("refresh_token"))
    except Exception:
        return False


def _save_xai_auth_json(tokens: dict) -> None:
    """Write xAI OAuth tokens to auth.json in hermes's expected format."""
    auth_path = Path(HERMES_HOME) / "auth.json"
    existing: dict = {}
    if auth_path.exists():
        try:
            existing = json.loads(auth_path.read_text())
        except Exception:
            pass
    if not isinstance(existing, dict):
        existing = {}

    providers = existing.setdefault("providers", {})
    providers["xai-oauth"] = {
        "tokens": tokens,
        "auth_mode": "oauth_device",
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "discovery": {
            "authorization_endpoint": "https://auth.x.ai/oauth2/authorize",
            "token_endpoint": _XAI_TOKEN_URL,
        },
        "redirect_uri": "",
    }
    existing["active_provider"] = "xai-oauth"
    existing["version"] = 2
    existing["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    auth_path.write_text(json.dumps(existing, indent=2) + "\n")
    try:
        auth_path.chmod(0o600)
    except Exception:
        pass


def _apply_xai_oauth_config(model: str) -> None:
    """Write config.yaml with provider=xai-oauth and the chosen model."""
    import yaml
    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if config_path.exists():
        try:
            with config_path.open() as f:
                loaded = yaml.safe_load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            pass

    merged = dict(existing)
    merged_model = dict(merged.get("model") if isinstance(merged.get("model"), dict) else {})
    if model:
        merged_model["default"] = model
    merged_model["provider"] = "xai-oauth"
    merged["model"] = merged_model

    merged_terminal = dict(merged.get("terminal") if isinstance(merged.get("terminal"), dict) else {})
    merged_terminal.setdefault("backend", "local")
    merged_terminal.setdefault("timeout", 60)
    merged_terminal.setdefault("cwd", "/tmp")
    merged["terminal"] = merged_terminal

    merged_agent = dict(merged.get("agent") if isinstance(merged.get("agent"), dict) else {})
    merged_agent.setdefault("max_iterations", 50)
    merged["agent"] = merged_agent
    merged["data_dir"] = HERMES_HOME

    with config_path.open("w") as f:
        yaml.safe_dump(merged, f, sort_keys=False, default_flow_style=False)

    # Persist LLM_MODEL and track the per-provider model so the setup UI can
    # display it alongside the xAI entry in the "Configured Providers" list.
    if model:
        existing_env = read_env(ENV_FILE)
        existing_env["LLM_MODEL"] = model
        existing_env["_MODEL_XAI_OAUTH"] = model
        write_env(ENV_FILE, existing_env)


async def _poll_xai_device_auth(state: dict) -> None:
    """Background task: poll xAI token endpoint until authorized or expired."""
    client = get_http_client()
    while time.time() < state["expires_at"]:
        await asyncio.sleep(state["interval"])
        try:
            resp = await client.post(
                _XAI_TOKEN_URL,
                data={
                    "grant_type": _XAI_GRANT_TYPE,
                    "device_code": state["device_code"],
                    "client_id": _XAI_CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=httpx.Timeout(15.0),
            )
        except Exception as e:
            print(f"[xai-oauth] poll error: {e!r}", flush=True)
            continue

        if resp.status_code == 200:
            try:
                tokens = resp.json()
            except Exception:
                state["status"] = "error"
                state["error"] = "Invalid token response from xAI"
                return
            _save_xai_auth_json(tokens)
            _apply_xai_oauth_config(state.get("model", ""))
            state["status"] = "authorized"
            print("[xai-oauth] authorized — restarting gateway", flush=True)
            asyncio.create_task(gw.restart())
            return

        try:
            err_data = resp.json()
        except Exception:
            err_data = {}
        error = err_data.get("error", "")

        if error == "authorization_pending":
            continue
        elif error == "slow_down":
            state["interval"] = min(state["interval"] + 5, 30)
        else:
            state["status"] = "error"
            state["error"] = err_data.get("error_description", error) or error or "Unknown error"
            print(f"[xai-oauth] failed: {error}", flush=True)
            return

    state["status"] = "expired"
    print("[xai-oauth] device code expired", flush=True)


async def api_oauth_xai_delete(request: Request) -> Response:
    global _xai_oauth_state
    if err := guard(request):
        return err
    auth_path = Path(HERMES_HOME) / "auth.json"
    if auth_path.exists():
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
            data.get("providers", {}).pop("xai-oauth", None)
            if data.get("active_provider") == "xai-oauth":
                data.pop("active_provider", None)
            auth_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        except Exception:
            pass
    env = read_env(ENV_FILE)
    env.pop("_MODEL_XAI_OAUTH", None)
    write_env(ENV_FILE, env)
    _xai_oauth_state = None
    return JSONResponse({"ok": True})


async def api_oauth_xai_start(request: Request) -> Response:
    global _xai_oauth_state
    if err := guard(request):
        return err

    try:
        body = await request.json()
    except Exception:
        body = {}
    model = str(body.get("model", "")).strip()

    client = get_http_client()
    try:
        resp = await client.post(
            _XAI_DEVICE_URL,
            data={"client_id": _XAI_CLIENT_ID, "scope": _XAI_SCOPE},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=httpx.Timeout(15.0),
        )
    except Exception as e:
        return JSONResponse({"error": f"Could not reach xAI: {e}"}, status_code=502)

    if resp.status_code != 200:
        return JSONResponse(
            {"error": f"xAI returned {resp.status_code}: {resp.text[:200]}"},
            status_code=502,
        )

    try:
        data = resp.json()
    except Exception:
        return JSONResponse({"error": "Invalid response from xAI"}, status_code=502)

    _xai_oauth_state = {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri_complete") or data["verification_uri"],
        "expires_at": time.time() + data.get("expires_in", 900),
        "interval": max(data.get("interval", 5), 5),
        "status": "pending",
        "model": model,
    }
    asyncio.create_task(_poll_xai_device_auth(_xai_oauth_state))

    return JSONResponse({
        "user_code": data["user_code"],
        "verification_uri": _xai_oauth_state["verification_uri"],
        "expires_in": data.get("expires_in", 900),
    })


async def api_oauth_xai_status(request: Request) -> Response:
    if err := guard(request):
        return err
    if _xai_oauth_state is None:
        # No active flow — check if a previous session left valid tokens.
        if _has_xai_oauth_tokens():
            return JSONResponse({"status": "authorized"})
        return JSONResponse({"status": "none"})
    return JSONResponse({
        "status": _xai_oauth_state["status"],
        "error": _xai_oauth_state.get("error", ""),
    })


def is_config_complete(data: dict[str, str] | None = None) -> bool:
    """Single source of truth for 'ready to run the gateway'.

    Used by: GET / redirect, auto_start on boot, admin API status.
    """
    if data is None:
        data = read_env(ENV_FILE)
    has_model = bool(data.get("LLM_MODEL"))
    has_provider = any(data.get(k) for k in PROVIDER_KEYS) or _has_xai_oauth_tokens()
    return has_model and has_provider


def mask(data: dict[str, str]) -> dict[str, str]:
    return {
        k: (v[:8] + "***" if len(v) > 8 else "***") if k in SECRET_KEYS and v else v
        for k, v in data.items()
    }


def unmask(new: dict[str, str], existing: dict[str, str]) -> dict[str, str]:
    return {
        k: (existing.get(k, "") if k in SECRET_KEYS and v.endswith("***") else v)
        for k, v in new.items()
    }


# ── Auth (cookie-based) ───────────────────────────────────────────────────────
# We use HMAC-signed cookies instead of HTTP Basic Auth because:
#   1. Basic auth's per-directory protection space means browsers cache creds
#      for /setup/* separately from /*, forcing re-prompt on navigation.
#   2. Browser behavior for sending Basic auth on XHR/fetch is inconsistent;
#      the Hermes React SPA's plain fetch() calls don't reliably include it,
#      causing every proxied API call to 401.
# Cookies are auto-included on every same-origin request (navigation + XHR)
# so both the setup UI and the proxied Hermes dashboard work with one login.
#
# The SECRET is regenerated on every process start. That means any ADMIN_PASSWORD
# change via Railway → redeploy → all existing cookies invalidate → users re-login.
import hashlib as _hashlib
import hmac as _hmac
from urllib.parse import quote as _url_quote, urlparse as _urlparse

COOKIE_NAME = "hermes_auth"
COOKIE_MAX_AGE = 7 * 86400  # 7 days
COOKIE_SECRET = secrets.token_bytes(32)

# Public paths — no auth required. Everything else is behind the cookie gate.
PUBLIC_PATHS = {"/health", "/login", "/logout"}


def _make_auth_token() -> str:
    """Build a cookie value: `<expires>.<hmac-sha256>`."""
    expires = str(int(time.time()) + COOKIE_MAX_AGE)
    sig = _hmac.new(COOKIE_SECRET, expires.encode(), _hashlib.sha256).hexdigest()
    return f"{expires}.{sig}"


def _verify_auth_token(token: str) -> bool:
    try:
        expires_s, sig = token.rsplit(".", 1)
        if int(expires_s) < time.time():
            return False
        expected = _hmac.new(COOKIE_SECRET, expires_s.encode(), _hashlib.sha256).hexdigest()
        return _hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _is_authenticated(request: Request) -> bool:
    return _verify_auth_token(request.cookies.get(COOKIE_NAME, ""))


def _safe_return_to(value: str) -> str:
    """Reject open-redirect attempts — only allow same-origin relative paths."""
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    # Strip any scheme/netloc that slipped through.
    p = _urlparse(value)
    if p.scheme or p.netloc:
        return "/"
    return value


def guard(request: Request) -> Response | None:
    """Enforce auth on protected routes.

    - HTML navigation: 302 to /login?returnTo=<path>
    - API / XHR: 401 JSON (so the SPA's fetch() can surface it cleanly)
    """
    if _is_authenticated(request):
        return None
    accept = request.headers.get("accept", "").lower()
    wants_html = "text/html" in accept
    if wants_html:
        rt = request.url.path
        if request.url.query:
            rt = f"{rt}?{request.url.query}"
        return RedirectResponse(f"/login?returnTo={_url_quote(rt)}", status_code=302)
    return JSONResponse({"error": "Unauthorized"}, status_code=401)


LOGIN_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hermes Agent — Sign in</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#c9d1d9;font-family:'IBM Plex Sans',sans-serif;
  min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px}
.card{background:#14181f;border:1px solid #252d3d;border-radius:12px;padding:36px 32px;width:100%;max-width:380px;
  box-shadow:0 20px 40px rgba(0,0,0,0.4)}
.brand{text-align:center;margin-bottom:28px}
.brand-logo{display:inline-flex;align-items:center;gap:10px;font-family:'IBM Plex Mono',monospace;font-weight:600;font-size:18px;color:#6272ff}
.brand-logo span{color:#6b7688;font-weight:400}
.brand-sub{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6b7688;margin-top:8px;letter-spacing:1.5px;text-transform:uppercase}
label{display:block;font-family:'IBM Plex Mono',monospace;font-size:11px;color:#6b7688;
  letter-spacing:0.05em;text-transform:uppercase;margin-bottom:6px;margin-top:16px}
input{width:100%;background:#0d0f14;border:1px solid #252d3d;border-radius:6px;color:#c9d1d9;
  font-family:'IBM Plex Mono',monospace;font-size:13px;padding:9px 11px;outline:none;transition:border-color .15s}
input:focus{border-color:#6272ff}
button{width:100%;margin-top:24px;background:#6272ff;border:1px solid #6272ff;border-radius:6px;color:#fff;
  font-family:'IBM Plex Mono',monospace;font-size:13px;font-weight:500;padding:10px;cursor:pointer;
  transition:background .15s,border-color .15s}
button:hover{background:#7b8fff;border-color:#7b8fff}
.err{background:rgba(248,81,73,0.08);border:1px solid rgba(248,81,73,0.3);border-radius:6px;
  color:#f85149;font-family:'IBM Plex Mono',monospace;font-size:12px;padding:8px 12px;margin-bottom:14px;text-align:center}
.footnote{margin-top:18px;font-family:'IBM Plex Mono',monospace;font-size:10px;color:#6b7688;text-align:center;line-height:1.6}
</style></head>
<body>
<div class="card">
  <div class="brand">
    <div class="brand-logo">hermes<span>/admin</span></div>
    <div class="brand-sub">Sign in to continue</div>
  </div>
  __ERROR__
  <form method="POST" action="/login">
    <input type="hidden" name="returnTo" value="__RETURN_TO__">
    <label for="username">Username</label>
    <input id="username" name="username" type="text" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Sign in</button>
  </form>
  <p class="footnote">Credentials are the <code>ADMIN_USERNAME</code> and <code>ADMIN_PASSWORD</code><br>Railway service variables.</p>
</div>
</body></html>"""


def _html_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


async def page_login(request: Request) -> Response:
    """GET /login — render the sign-in form."""
    # Already signed in? Bounce to returnTo (or /).
    if _is_authenticated(request):
        return RedirectResponse(_safe_return_to(request.query_params.get("returnTo", "/")), status_code=302)
    rt = _safe_return_to(request.query_params.get("returnTo", "/"))
    error_html = ('<div class="err">Invalid username or password</div>'
                  if request.query_params.get("error") else "")
    html = (LOGIN_PAGE_HTML
            .replace("__ERROR__", error_html)
            .replace("__RETURN_TO__", _html_escape(rt)))
    return HTMLResponse(html)


async def login_post(request: Request) -> Response:
    """POST /login — validate creds and set the auth cookie."""
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    return_to = _safe_return_to(str(form.get("returnTo", "/")))

    valid_user = _hmac.compare_digest(username, ADMIN_USERNAME)
    valid_pw = _hmac.compare_digest(password, ADMIN_PASSWORD)
    if valid_user and valid_pw:
        resp = RedirectResponse(return_to, status_code=302)
        resp.set_cookie(
            COOKIE_NAME,
            _make_auth_token(),
            max_age=COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp
    return RedirectResponse(f"/login?returnTo={_url_quote(return_to)}&error=1", status_code=302)


async def logout(request: Request) -> Response:
    """GET /logout — clear cookie and bounce to login."""
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


# ── Gateway manager ───────────────────────────────────────────────────────────
# Auto-respawn tuning. When the gateway exits without us asking it to — an
# in-band `/restart` (inside a container hermes exits 75 expecting a supervisor
# to bring it back; verified it takes the exit-75 path, NOT a detached
# self-restart, when /run/.containerenv or /.dockerenv exists), a crash, or an
# OOM kill — server.py is that supervisor and must restart it. Nothing else
# will, and /health stays 200, so the bot would otherwise sit silently dead.
# A crash-loop guard stops us hammering a gateway that genuinely can't stay up
# (e.g. a bad provider key / model).
RESPAWN_WINDOW_S   = 120     # rolling window (s) for counting unexpected exits
RESPAWN_MAX_IN_WIN = 5       # give up auto-restart after this many exits in window
RESPAWN_BASE_DELAY = 2.0     # first backoff (seconds)
RESPAWN_MAX_DELAY  = 30.0    # backoff cap


class Gateway:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.started_at: float | None = None
        self.restarts = 0
        # True while a deliberate stop()/restart()/reset is in flight, so the
        # exiting process's _drain() doesn't fire an auto-respawn that races the
        # intentional lifecycle.
        self._stopping = False
        # Monotonic timestamps of recent unexpected exits (crash-loop guard).
        self._recent_exits: list[float] = []

    async def start(self, *, reset_budget: bool = True):
        if self.proc and self.proc.returncode is None:
            return
        # A manual Start/Restart (or boot) grants a fresh crash-loop budget; the
        # auto-respawn path passes reset_budget=False so repeated crashes keep
        # accumulating toward the give-up threshold.
        if reset_budget:
            self._recent_exits.clear()
        self.state = "starting"
        self._stopping = False
        try:
            # .env values take priority over Railway env vars.
            # We build the env this way so hermes's own dotenv loading
            # (which reads the same file) doesn't shadow our values.
            env = {**os.environ, "HERMES_HOME": HERMES_HOME}
            env.update(read_env(ENV_FILE))
            model = env.get("LLM_MODEL", "")
            provider_key = next((env.get(k, "") for k in PROVIDER_KEYS if env.get(k)), "")
            print(f"[gateway] model={model or '⚠ NOT SET'} | provider_key={'set' if provider_key else '⚠ NOT SET'}", flush=True)
            # Write config.yaml so hermes picks up the model (env vars alone aren't always enough)
            write_config_yaml(read_env(ENV_FILE))
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.started_at = time.time()
            asyncio.create_task(self._drain(self.proc))
        except Exception as e:
            self.state = "error"
            self.logs.append(f"[error] Failed to start: {e}")

    async def stop(self):
        self._stopping = True
        if not self.proc or self.proc.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.state = "stopped"
        self.started_at = None

    async def restart(self):
        await self.stop()
        self.restarts += 1
        await self.start()

    async def _drain(self, proc: asyncio.subprocess.Process):
        assert proc.stdout
        async for raw in proc.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
            self.logs.append(line)
        rc = proc.returncode
        # Ignore the drain of a process we've already replaced (e.g. via restart()).
        if proc is not self.proc:
            return
        # A deliberate stop()/restart()/reset owns its own lifecycle — don't respawn.
        if self._stopping:
            return
        # Unexpected exit: in-band `/restart` (exit 75), a crash, or an OOM kill.
        # On Railway nothing else brings the gateway back, so we supervise it.
        self.state = "error"
        self.logs.append(f"[gateway] exited (code {rc}) — supervising restart")
        asyncio.create_task(self._supervise_respawn(proc.pid))

    async def _supervise_respawn(self, dead_pid: int | None):
        # Crash-loop guard: count unexpected exits inside a rolling window and
        # give up (rather than hammer) once they exceed the threshold.
        now = time.monotonic()
        self._recent_exits = [t for t in self._recent_exits if now - t < RESPAWN_WINDOW_S]
        self._recent_exits.append(now)
        if len(self._recent_exits) > RESPAWN_MAX_IN_WIN:
            self.state = "crashed"
            self.logs.append(
                f"[gateway] crash-looping ({len(self._recent_exits)} exits in "
                f"{RESPAWN_WINDOW_S}s) — giving up auto-restart. Fix the provider/"
                f"model in the admin UI, then Start/Restart the gateway."
            )
            return
        delay = min(RESPAWN_BASE_DELAY * 2 ** (len(self._recent_exits) - 1), RESPAWN_MAX_DELAY)
        self.logs.append(f"[gateway] restarting in {int(delay)}s (attempt {len(self._recent_exits)})")
        await asyncio.sleep(delay)
        # Re-check the deliberate-lifecycle conditions AFTER the backoff sleep: a
        # Stop, Reset, or shutdown issued during the wait must win over the respawn.
        if self._stopping:
            self.logs.append("[gateway] restart cancelled (stopped/reconfigured)")
            return
        if self.proc and self.proc.returncode is None:
            return  # a manual Start already brought a live gateway back
        if not is_config_complete():
            self.state = "stopped"
            self.logs.append("[gateway] restart skipped — provider/model not configured")
            return
        # Clear a pid file left stale by a hard crash (SIGKILL/OOM skips hermes'
        # atexit cleanup) so the respawn's own O_EXCL pid claim can't bail with
        # "PID file race lost". Scoped to the pid we just buried — never disturbs
        # a live gateway's lock.
        self._clear_stale_pidfile(dead_pid)
        self.restarts += 1
        await self.start(reset_budget=False)

    def _clear_stale_pidfile(self, dead_pid: int | None) -> None:
        if dead_pid is None:
            return
        pid_file = Path(HERMES_HOME) / "gateway.pid"
        try:
            rec = json.loads(pid_file.read_text())
        except Exception:
            return
        if rec.get("pid") == dead_pid:
            try:
                pid_file.unlink()
                self.logs.append(f"[gateway] cleared stale pid file (pid {dead_pid})")
            except OSError:
                pass

    def status(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.state == "running" else None
        return {
            "state":    self.state,
            "pid":      self.proc.pid if self.proc and self.proc.returncode is None else None,
            "uptime":   uptime,
            "restarts": self.restarts,
        }


gw = Gateway()
cfg_lock = asyncio.Lock()


# ── Hermes serve subprocess ───────────────────────────────────────────────────
def _serve_auth_configured() -> bool:
    """True when a Hermes dashboard auth provider is configured for serve."""
    data = read_env(ENV_FILE) if ENV_FILE.exists() else {}
    merged = {**data, **{k: v for k, v in os.environ.items() if k.startswith("HERMES_DASHBOARD_")}}
    if merged.get("HERMES_DASHBOARD_OAUTH_CLIENT_ID") or merged.get("HERMES_DASHBOARD_OIDC_ISSUER"):
        return True
    user = (merged.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or "").strip()
    if not user:
        return False
    return bool(
        merged.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD")
        or merged.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD_HASH")
    )


class Serve:
    """Manages the `hermes serve` subprocess (headless backend for Desktop + web UI).

    Caddy exposes this on Railway's $PORT. Hermes auth (basic or OAuth) must be
    configured in /data/.hermes/.env when binding a non-loopback address.
    """

    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.logs: deque[str] = deque(maxlen=300)
        self._drain_task: asyncio.Task | None = None

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        if HERMES_SERVE_HOST not in ("127.0.0.1", "localhost", "::1") and not _serve_auth_configured():
            print(
                "[serve] WARNING: binding to "
                f"{HERMES_SERVE_HOST}:{HERMES_SERVE_PORT} without dashboard auth. "
                "Set HERMES_DASHBOARD_BASIC_AUTH_* or HERMES_DASHBOARD_OAUTH_CLIENT_ID "
                "in /data/.hermes/.env — serve may refuse to start.",
                flush=True,
            )
        try:
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "serve",
                "--host", HERMES_SERVE_HOST,
                "--port", str(HERMES_SERVE_PORT),
                "--no-open",
                "--skip-build",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            print(f"[serve] spawned pid={self.proc.pid} → {HERMES_SERVE_URL}", flush=True)
            self._drain_task = asyncio.create_task(self._drain())
        except Exception as e:
            print(f"[serve] FAILED to spawn: {e!r}", flush=True)

    async def _drain(self):
        """Stream subprocess output to Railway logs (prefixed) and a ring buffer."""
        assert self.proc and self.proc.stdout
        try:
            async for raw in self.proc.stdout:
                line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
                self.logs.append(line)
                print(f"[serve] {line}", flush=True)
        except Exception as e:
            print(f"[serve] drain error: {e!r}", flush=True)
        finally:
            rc = self.proc.returncode if self.proc else None
            if rc is not None and rc != 0:
                print(f"[serve] EXITED with code {rc}", flush=True)
            elif rc == 0:
                print(f"[serve] exited cleanly (code 0)", flush=True)

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            return
        if self._drain_task:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()


serve = Serve()

# Shared async HTTP client for OAuth polling. Created lazily, torn down in lifespan.
_http_client: httpx.AsyncClient | None = None


def get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            follow_redirects=False,
        )
    return _http_client


# ── Route handlers ────────────────────────────────────────────────────────────
async def page_index(request: Request):
    if err := guard(request): return err
    return templates.TemplateResponse(request, "index.html")


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state})


async def api_config_get(request: Request):
    if err := guard(request): return err
    async with cfg_lock:
        data = read_env(ENV_FILE)
    defs = [{"key": k, "label": l, "category": c, "secret": s} for k, l, c, s in ENV_VARS]
    return JSONResponse({"vars": mask(data), "defs": defs})


async def api_config_put(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        restart = body.pop("_restart", False)
        new_vars = body.get("vars", {})
        async with cfg_lock:
            existing = read_env(ENV_FILE)
            merged = unmask(new_vars, existing)
            for k, v in existing.items():
                if k not in merged:
                    merged[k] = v
            write_env(ENV_FILE, merged)
            write_config_yaml(merged)
        if restart:
            asyncio.create_task(gw.restart())
        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    providers = {
        k.replace("_API_KEY","").replace("_TOKEN","").replace("HF_","HuggingFace ").replace("_"," ").title():
        {"configured": bool(data.get(k))}
        for k in PROVIDER_KEYS
    }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    return JSONResponse({"gateway": gw.status(), "providers": providers, "channels": channels})


async def api_logs(request: Request):
    if err := guard(request): return err
    return JSONResponse({"lines": list(gw.logs)})


async def api_gw_start(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.start())
    return JSONResponse({"ok": True})


async def api_gw_stop(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    return JSONResponse({"ok": True})


async def api_gw_restart(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.restart())
    return JSONResponse({"ok": True})


async def api_config_reset(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    async with cfg_lock:
        if ENV_FILE.exists():
            ENV_FILE.unlink()
        write_config_yaml({})
    return JSONResponse({"ok": True})


# ── Pairing ───────────────────────────────────────────────────────────────────
# Pending-request file format (hermes >= v0.15 / v2026.5.29.x, gateway/pairing.py):
# each `{platform}-pending.json` entry is keyed by a random opaque `entry_id`
# (secrets.token_hex), and the user-facing pairing code is stored only as a
# salted hash ({hash, salt, user_id, user_name, created_at}) — the plaintext
# code is never on disk. Our admin-approval flow is code-agnostic: the dashboard
# is already cookie-authed, so we approve by moving an entry from pending →
# approved keyed off that `entry_id` (round-tripped from the pending list as
# `code`), reading `user_id`/`user_name` straight from the entry. We must NOT
# uppercase that key — entry_ids are lowercase hex, and uppercasing them was
# what silently broke approve/deny on the v0.15 upgrade. Older plaintext-keyed
# entries still work here because we treat the key as an opaque handle.
def _pjson(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _wjson(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try: os.chmod(path, 0o600)
    except OSError: pass


def _platforms(suffix: str) -> list[str]:
    if not PAIRING_DIR.exists(): return []
    return [f.stem.rsplit(f"-{suffix}", 1)[0] for f in PAIRING_DIR.glob(f"*-{suffix}.json")]


async def api_pairing_pending(request: Request):
    if err := guard(request): return err
    now = time.time()
    out = []
    for p in _platforms("pending"):
        for code, info in _pjson(PAIRING_DIR / f"{p}-pending.json").items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                out.append({"platform": p, "code": code,
                            "user_id": info.get("user_id",""), "user_name": info.get("user_name",""),
                            "age_minutes": int((now - info.get("created_at", now)) / 60)})
    return JSONResponse({"pending": out})


async def api_pairing_approve(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)
    entry = pending.pop(code)
    user_id = (entry.get("user_id") or "").strip() if isinstance(entry, dict) else ""
    if not user_id:
        # Malformed/legacy entry without a user_id — leave it in pending (we
        # haven't written the pop yet) rather than silently discarding it.
        return JSONResponse({"error": "Pending entry has no user_id"}, status_code=422)
    _wjson(pending_path, pending)
    approved = _pjson(PAIRING_DIR / f"{platform}-approved.json")
    approved[user_id] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    _wjson(PAIRING_DIR / f"{platform}-approved.json", approved)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").strip()
    p = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(p)
    if code in pending:
        del pending[code]
        _wjson(p, pending)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(PAIRING_DIR / f"{p}-approved.json").items():
            out.append({"platform": p, "user_id": uid,
                        "user_name": info.get("user_name",""), "approved_at": info.get("approved_at",0)})
    return JSONResponse({"approved": out})


async def api_pairing_revoke(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, uid = body.get("platform",""), body.get("user_id","")
    if not platform or not uid:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    p = PAIRING_DIR / f"{platform}-approved.json"
    approved = _pjson(p)
    if uid in approved:
        del approved[uid]
        _wjson(p, approved)
    return JSONResponse({"ok": True})


async def route_setup_404(request: Request) -> Response:
    """Typos under /setup/* should 404 here."""
    if err := guard(request): return err
    return Response("Not Found", status_code=404, media_type="text/plain")


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def auto_start():
    if is_config_complete():
        asyncio.create_task(gw.start())
    else:
        print("[server] Config incomplete — gateway not started. Configure provider + model in the admin UI.", flush=True)


@asynccontextmanager
async def lifespan(app):
    asyncio.create_task(serve.start())
    await auto_start()
    try:
        yield
    finally:
        await asyncio.gather(
            gw.stop(),
            serve.stop(),
            return_exceptions=True,
        )
        global _http_client
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None


ANY_METHOD = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

routes = [
    # Public — no auth required.
    Route("/health",                            route_health),
    Route("/login",                             page_login,          methods=["GET"]),
    Route("/login",                             login_post,          methods=["POST"]),
    Route("/logout",                            logout),

    # Setup wizard + management API under /setup/* (cookie-auth guarded).
    Route("/setup",                             page_index),
    Route("/setup/",                            page_index),
    Route("/setup/api/config",                  api_config_get,      methods=["GET"]),
    Route("/setup/api/config",                  api_config_put,      methods=["PUT"]),
    Route("/setup/api/status",                  api_status),
    Route("/setup/api/logs",                    api_logs),
    Route("/setup/api/gateway/start",           api_gw_start,        methods=["POST"]),
    Route("/setup/api/gateway/stop",            api_gw_stop,         methods=["POST"]),
    Route("/setup/api/gateway/restart",         api_gw_restart,      methods=["POST"]),
    Route("/setup/api/config/reset",            api_config_reset,    methods=["POST"]),
    Route("/setup/api/pairing/pending",         api_pairing_pending),
    Route("/setup/api/pairing/approve",         api_pairing_approve, methods=["POST"]),
    Route("/setup/api/pairing/deny",            api_pairing_deny,    methods=["POST"]),
    Route("/setup/api/pairing/approved",        api_pairing_approved),
    Route("/setup/api/pairing/revoke",          api_pairing_revoke,  methods=["POST"]),
    Route("/setup/api/oauth/xai/start",         api_oauth_xai_start,  methods=["POST"]),
    Route("/setup/api/oauth/xai/status",        api_oauth_xai_status),
    Route("/setup/api/oauth/xai",               api_oauth_xai_delete, methods=["DELETE"]),
    Route("/setup/{path:path}",                 route_setup_404,     methods=ANY_METHOD),
]

# Auth enforced per-handler via guard(). Caddy routes /setup and /health here;
# all other paths go to `hermes serve` on HERMES_SERVE_PORT.
app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    # Caddy owns Railway's $PORT; admin binds ADMIN_INTERNAL_PORT only.
    port = int(os.environ.get("ADMIN_INTERNAL_PORT", "8080"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def _shutdown():
        loop.create_task(gw.stop())
        loop.create_task(serve.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    loop.run_until_complete(server.serve())
