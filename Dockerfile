FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Which hermes-agent revision to install. Accepts any git ref the upstream
# repo publishes — a release tag (recommended for reproducibility) or a
# branch name (`main`) for bleeding edge.
#
# To bump: check https://github.com/NousResearch/hermes-agent/releases for the
# newest tag (format `vYYYY.M.D`, optionally with a `.PATCH` suffix, e.g.
# `v2026.5.29.2`) and update the default below. Use `main` only if you accept
# that every rebuild can pull arbitrary new upstream commits.
ARG HERMES_REF=v2026.6.19

# tini = tiny init that we run as PID 1. Without it, hermes's grandchild
# processes (MCP stdio servers, git, bun, browser daemons spawned by tools)
# reparent to PID 1 when their parents exit and pile up as zombies. After
# weeks of uptime that exhausts the kernel's PID table → "fork: cannot
# allocate memory" and the container dies. tini reaps zombies in the
# background and forwards SIGTERM/SIGINT to our entrypoint so Railway's
# stop signal still triggers our graceful shutdown. Standard container init
# (same as Docker's `--init` flag and Kubernetes' pause container).
#
# Node.js is required at build time (Hermes React/TUI dashboards) and at runtime
# for the Raft platform CLI (`raft agent bridge`, `raft message *`).
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git tini && \
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    rm -rf /var/lib/apt/lists/*

# Caddy — optional host-based edge proxy when PROXY_HOST_ROUTES is set (Railway
# exposes one $PORT; Caddy routes custom domains to internal Hermes API ports).
ARG CADDY_VERSION=2.9.1
RUN curl -fsSL "https://github.com/caddyserver/caddy/releases/download/v${CADDY_VERSION}/caddy_${CADDY_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C /usr/bin caddy

# Install hermes-agent (provides the `hermes` CLI) and pre-build its React
# dashboard so `hermes dashboard` has nothing to build at runtime.
#
# [all] in v2026.6.5 no longer pulls in [dev]; messaging platforms, TTS, and
# other heavy backends are lazy-installed by hermes at first use. We pre-install
# the ones this template actually uses so first-message latency is instant.
# `vision` (Pillow) is a soft-dep that is NOT in [all] and is otherwise
# lazy-installed at first image use: without it hermes can't downscale an
# oversized image (>5 MB / >8000px), which then bakes into immutable history
# and bricks the session on Anthropic's non-retryable 400. We bake it in.
# When bumping HERMES_REF, re-check hermes-agent's pyproject.toml [all] and
# the extras below against the new release's pyproject.toml.
RUN git clone --depth 1 --branch ${HERMES_REF} https://github.com/NousResearch/hermes-agent.git /opt/hermes-agent && \
    cd /opt/hermes-agent && \
    uv pip install --system --no-cache -e ".[all,messaging,tts-premium,honcho,bedrock,anthropic,edge-tts,hindsight,vision]" && \
    cd /opt/hermes-agent/web && \
    npm install --silent && \
    npm run build && \
    cd /opt/hermes-agent/ui-tui && \
    npm install --silent --no-fund --no-audit --progress=false && \
    npm run build && \
    rm -rf /opt/hermes-agent/web /opt/hermes-agent/.git /root/.npm

# Runtime file-editing utilities (must survive in the final image — not build-only).
# - ripgrep: backs Hermes search_files (without it the tool falls back to slower grep
#   or fails on wide trees). Shipped in the official nousresearch/hermes-agent image.
# - vim: $EDITOR for docker exec, hermes config edit, and TUI Ctrl+G.
# - patch: GNU patch for terminal workflows that apply unified diffs outside the
#   Python patch tool.
# - jq: JSON wrangling for the agent + used below to resolve the midas release asset.
# - xz-utils: tar can't extract the .tar.xz midas release archives without it.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ripgrep vim patch jq xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Raft platform CLI (https://raft.build). Hermes' raft adapter checks PATH for
# `raft`, spawns `raft agent bridge`, and expects the agent to use `raft message`
# commands. Pin with RAFT_CLI_VERSION build-arg; bump from npm when upgrading.
# Setup after deploy: `raft agent login ...` then set RAFT_PROFILE in /data/.hermes/.env
ARG RAFT_CLI_VERSION=0.0.15
RUN npm install -g "@botiverse/raft@${RAFT_CLI_VERSION}" && \
    npm cache clean --force

# Obsidian headless client (https://obsidian.md/help/publish/headless). Provides
# the `ob` binary — the ONLY Obsidian CLI that runs without the desktop app.
# One package covers login, Sync, AND Publish (`ob login`, `ob sync`, `ob publish`).
# Requires Node 22+ (installed above). Currently in open beta; pin and bump the
# ARG deliberately after checking `npm view obsidian-headless version`.
#
# Setup after deploy (one time — HOME=/data is the persistent volume, so the
# credentials in ~/.config survive restarts and redeploys):
#   ob login
#   ob publish-list-sites            # then, inside the vault directory:
#   ob publish-setup --site "<site>" # and `ob publish` to deploy
# Requires an active Obsidian Publish (and/or Sync) subscription.
ARG OBSIDIAN_HEADLESS_VERSION=0.0.12
RUN npm install -g "obsidian-headless@${OBSIDIAN_HEADLESS_VERSION}" && \
    npm cache clean --force

# midas — Midian's CLI (private repo: github.com/midianio/midas). Prebuilt Linux
# binaries are published to GitHub releases by cargo-dist, so we download instead
# of compiling Rust here.
#
# MIDAS_GITHUB_TOKEN: Railway doesn't support BuildKit `--mount=type=secret`, so
# the token comes in as a build ARG (declare it as a Railway service variable and
# it's injected at build time). ARG values used in RUN are recoverable from image
# history — use a fine-grained PAT scoped to ONLY midianio/midas with read-only
# Contents permission, nothing else, and rotate it periodically. Leave it unset
# to skip the midas install entirely (image still builds).
ARG MIDAS_VERSION=0.1.2
ARG MIDAS_GITHUB_TOKEN=
RUN set -eu; \
    if [ -z "${MIDAS_GITHUB_TOKEN}" ]; then \
        echo "MIDAS_GITHUB_TOKEN not set — skipping midas install"; exit 0; \
    fi; \
    arch="$(uname -m)"; \
    case "$arch" in x86_64|aarch64) ;; *) echo "unsupported arch: $arch" >&2; exit 1 ;; esac; \
    asset="midas-${arch}-unknown-linux-gnu.tar.xz"; \
    asset_id="$(curl -fsSL -H "Authorization: Bearer ${MIDAS_GITHUB_TOKEN}" \
        "https://api.github.com/repos/midianio/midas/releases/tags/${MIDAS_VERSION}" \
        | jq -r --arg name "$asset" '.assets[] | select(.name == $name) | .id')"; \
    [ -n "$asset_id" ] && [ "$asset_id" != "null" ] || { echo "asset $asset not found in release ${MIDAS_VERSION}" >&2; exit 1; }; \
    curl -fsSL -H "Authorization: Bearer ${MIDAS_GITHUB_TOKEN}" \
        -H "Accept: application/octet-stream" \
        "https://api.github.com/repos/midianio/midas/releases/assets/${asset_id}" \
        -o /tmp/midas.tar.xz; \
    tar -xJf /tmp/midas.tar.xz -C /tmp; \
    install -m 0755 "/tmp/midas-${arch}-unknown-linux-gnu/midas" /usr/local/bin/midas; \
    rm -rf /tmp/midas.tar.xz "/tmp/midas-${arch}-unknown-linux-gnu"; \
    midas --version

# Why pre-build ui-tui (and why we don't delete it after):
# - The dashboard's embedded Chat tab spawns `node ui-tui/dist/entry.js`
#   on every WebSocket connect to /api/pty.
# - Without HERMES_TUI_DIR, hermes's _make_tui_argv falls through to the
#   npm install + build path (since git-editable installs don't have the
#   bundled tui_dist/ that PyPI wheels include), adding 30-60s to the
#   first chat-open and blocking the asyncio event loop.
# - Pre-building at image time surfaces build failures here rather than
#   at user request time, and makes first-chat-open instant.
# - We keep ui-tui/ entirely (node_modules + dist + src) so HERMES_TUI_DIR
#   can point at it (see below).

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

COPY server.py /app/server.py
COPY templates/ /app/templates/
COPY scripts/ /app/scripts/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh /app/scripts/render-caddyfile.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

# Points hermes at our pre-built TUI bundle. hermes's _make_tui_argv checks
# HERMES_TUI_DIR first: if dist/entry.js exists there, it skips the npm
# install/build entirely. This is the official packager path (Nix uses it too)
# and avoids the 30-60s npm bootstrap that git-editable installs would otherwise
# trigger on first /chat connection.
ENV HERMES_TUI_DIR=/opt/hermes-agent/ui-tui
ENV EDITOR=vim

# tini wraps start.sh so it runs as PID 1's child instead of as PID 1 itself.
# `-g` propagates signals to the whole process group so `docker stop` /
# Railway's SIGTERM cleanly terminates the entire tree, not just start.sh.
ENTRYPOINT ["/usr/bin/tini", "-g", "--"]
CMD ["/app/start.sh"]
