#!/bin/bash
# Generate /tmp/Caddyfile — Caddy listens on Railway's $PORT and routes:
#   /setup, /health, /login  → admin server (ADMIN_INTERNAL_PORT)
#   optional host routes     → Hermes API servers (PROXY_HOST_ROUTES)
#   everything else          → hermes serve (HERMES_SERVE_PORT) for Desktop + web UI
set -euo pipefail

ADMIN_PORT="${ADMIN_INTERNAL_PORT:-8080}"
SERVE_PORT="${HERMES_SERVE_PORT:-9120}"
LISTEN_PORT="${PORT:-9119}"
ROUTES="${PROXY_HOST_ROUTES:-}"

{
  printf '%s\n' '{' '    auto_https off' '    admin off' '}'
  printf '\n:%s {\n' "${LISTEN_PORT}"

  printf '    @admin path /health /health/* /login /login/* /logout /logout/* /setup /setup/*\n'
  printf '    handle @admin {\n'
  printf '        reverse_proxy 127.0.0.1:%s\n' "${ADMIN_PORT}"
  printf '    }\n\n'

  idx=0
  if [ -n "${ROUTES}" ]; then
    IFS=',' read -ra PAIRS <<< "${ROUTES}"
    for pair in "${PAIRS[@]}"; do
      pair="${pair#"${pair%%[![:space:]]*}"}"
      pair="${pair%"${pair##*[![:space:]]}"}"
      [ -z "${pair}" ] && continue
      host="${pair%%:*}"
      port="${pair#*:}"
      if [ -z "${host}" ] || [ -z "${port}" ] || [ "${host}" = "${port}" ]; then
        echo "[caddy] skipping invalid PROXY_HOST_ROUTES entry: ${pair}" >&2
        continue
      fi
      idx=$((idx + 1))
      printf '    @route%d host %s\n' "${idx}" "${host}"
      printf '    handle @route%d {\n' "${idx}"
      printf '        reverse_proxy 127.0.0.1:%s\n' "${port}"
      printf '    }\n\n'
    done
  fi

  printf '    handle {\n'
  printf '        reverse_proxy 127.0.0.1:%s\n' "${SERVE_PORT}"
  printf '    }\n'
  printf '}\n'
} > /tmp/Caddyfile

echo "[caddy] wrote /tmp/Caddyfile (listen :${LISTEN_PORT}, admin → :${ADMIN_PORT}, serve → :${SERVE_PORT}, ${idx} API route(s))" >&2
