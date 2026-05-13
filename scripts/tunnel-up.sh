#!/usr/bin/env bash
# tunnel-up.sh — one-line "just run the tunnel" wrapper.
#
# Reads ~/.cloudflared/config.yml to find the tunnel name, then runs
# `cloudflared tunnel run <name>` in the foreground. If no config exists,
# hands off to tunnel-config.sh.

set -e

cd "$(dirname "$0")/.."

trap 'rc=$?; printf "\n[tunnel-up] aborted (exit %d) on line %d\n" "$rc" "$LINENO" >&2; exit $rc' ERR

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_GREEN="$(tput setaf 2)"; C_YELLOW="$(tput setaf 3)"; C_RED="$(tput setaf 1)"
  C_BLUE="$(tput setaf 4)"; C_BOLD="$(tput bold)"; C_DIM="$(tput dim 2>/dev/null || echo '')"
  C_RESET="$(tput sgr0)"
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_BOLD=""; C_DIM=""; C_RESET=""
fi

CFD_DIR="$HOME/.cloudflared"
CONFIG_PATH="$CFD_DIR/config.yml"

if ! command -v cloudflared >/dev/null 2>&1; then
  printf "%s✗%s cloudflared not installed.\n" "$C_RED" "$C_RESET"
  printf "  Run the wizard first: bash scripts/tunnel-config.sh\n"
  exit 1
fi

if [ ! -f "$CONFIG_PATH" ]; then
  printf "%s⚠%s no $CONFIG_PATH — handing off to the setup wizard.\n" "$C_YELLOW" "$C_RESET"
  exec bash scripts/tunnel-config.sh
fi

# Pull tunnel name from the YAML — first "tunnel: <name>" line at column 0.
# Tolerate quoted forms and trailing whitespace; ignore comment lines.
TUNNEL_NAME="$(grep -E '^[[:space:]]*tunnel:[[:space:]]*[^#]' "$CONFIG_PATH" \
  | head -n 1 \
  | sed -E 's/^[[:space:]]*tunnel:[[:space:]]*//; s/[[:space:]]*$//; s/^[\"'\'']//; s/[\"'\'']$//' \
  || true)"

if [ -z "$TUNNEL_NAME" ]; then
  printf "%s✗%s could not find a 'tunnel:' line in %s\n" "$C_RED" "$C_RESET" "$CONFIG_PATH"
  printf "  Re-run the wizard: bash scripts/tunnel-config.sh\n"
  exit 1
fi

# Quick heads-up about the local server. We don't *require* it to be up
# (cloudflared will just 502 until it is), but the user almost always wants it.
if command -v curl >/dev/null 2>&1; then
  HEALTH_CODE="$(curl -fsS -o /dev/null -w "%{http_code}" \
    --max-time 2 http://localhost:8765/healthz 2>/dev/null || echo 000)"
  if [ "$HEALTH_CODE" = "000" ]; then
    printf "%s⚠%s dashboard server is not responding on localhost:8765.\n" "$C_YELLOW" "$C_RESET"
    printf "  Start it in another terminal:\n"
    printf "      HOST=0.0.0.0 .venv/bin/python server.py\n"
    printf "  (Continuing anyway — the tunnel will 502 until the server is up.)\n\n"
  fi
fi

printf "%s==>%s starting tunnel %s%s%s (foreground)\n" \
  "$C_BLUE" "$C_RESET" "$C_BOLD" "$TUNNEL_NAME" "$C_RESET"
printf "%s    Ctrl+C to stop. For boot-time launch, run: sudo cloudflared service install%s\n\n" \
  "$C_DIM" "$C_RESET"

# exec so signals (Ctrl+C) reach cloudflared directly without the wrapper script
# in between.
exec cloudflared tunnel run "$TUNNEL_NAME"
