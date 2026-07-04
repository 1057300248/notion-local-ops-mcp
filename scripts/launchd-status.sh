#!/usr/bin/env bash
set -euo pipefail

# shellcheck disable=SC1091
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/launchd-common.sh"

prepare_launchd_env
require_command launchctl

resolve_host_system() {
  local host="$1"
  if command -v dig >/dev/null 2>&1; then
    dig +time=3 +tries=1 +short "${host}" A 2>/dev/null | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' && return 0
    dig +time=3 +tries=1 +short "${host}" AAAA 2>/dev/null | grep -Eq ':' && return 0
    return 1
  fi
  return 0
}

for label in "$(mcp_label)" "$(cloudflared_label)" "$(watchdog_label)"; do
  target="$(launchctl_target "${label}")"
  echo "=== ${target} ==="
  if launchctl print "${target}" >/tmp/notion-local-ops-launchctl.print 2>&1; then
    sed -n '1,40p' /tmp/notion-local-ops-launchctl.print
  else
    cat /tmp/notion-local-ops-launchctl.print
  fi
  echo
  rm -f /tmp/notion-local-ops-launchctl.print
done

echo "=== local MCP ==="
if curl -fsSI "http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}/mcp" >/dev/null 2>&1; then
  curl -sSI "http://${NOTION_LOCAL_OPS_HOST}:${NOTION_LOCAL_OPS_PORT}/mcp" | sed -n '1,12p'
else
  echo "Local /mcp is not reachable"
fi

if CLOUDFLARED_CONFIG="$(pick_cloudflared_config 2>/dev/null || true)"; then
  hostname=$(awk '/hostname:/{print $3; exit}' "${CLOUDFLARED_CONFIG}" 2>/dev/null || true)
  if [[ -n "${hostname}" ]]; then
    echo
    echo "=== public MCP ==="
    if resolve_host_system "${hostname}"; then
      echo "DNS OK: ${hostname}"
    else
      echo "DNS FAIL: system resolver cannot resolve ${hostname}"
    fi
    if curl -fsSI --max-time 10 "https://${hostname}/mcp" >/dev/null 2>&1; then
      curl -sSI --max-time 10 "https://${hostname}/mcp" | sed -n '1,12p'
    else
      echo "Public /mcp is not reachable at https://${hostname}/mcp"
    fi
  fi
fi

echo
echo "Logs: ${NOTION_LOCAL_OPS_LAUNCHD_LOG_DIR}"
echo
echo "Run ./scripts/launchd-doctor.sh --fix for an immediate health check and targeted restart."
