#!/bin/bash
# Generate /tmp/Caddyfile for host-based routing on Railway's single $PORT.
#
# PROXY_HOST_ROUTES — comma-separated host:port pairs, e.g.
#   api-one.example.com:8642,api-two.example.com:8643
# Each hostname is matched on the Host header (port suffix ignored by Caddy).
# Unmatched hosts fall through to the admin server on ADMIN_INTERNAL_PORT.
set -euo pipefail

ADMIN_PORT="${ADMIN_INTERNAL_PORT:-8080}"
LISTEN_PORT="${PORT:-8080}"
ROUTES="${PROXY_HOST_ROUTES:-}"

{
  printf '%s\n' '{' '    auto_https off' '    admin off' '}'
  printf '\n:%s {\n' "${LISTEN_PORT}"

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
  printf '        reverse_proxy 127.0.0.1:%s\n' "${ADMIN_PORT}"
  printf '    }\n'
  printf '}\n'
} > /tmp/Caddyfile

echo "[caddy] wrote /tmp/Caddyfile (listen :${LISTEN_PORT}, ${idx} host route(s), default → :${ADMIN_PORT})" >&2
