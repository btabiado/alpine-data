#!/usr/bin/env bash
# tunnel-config.sh — one-time wizard for a named Cloudflare Tunnel.
#
# Walks through: install check → login → tunnel create → config.yml → DNS route.
# Idempotent: re-running on a partially set-up machine resumes where you left
# off rather than starting over. Every cloudflared invocation echoes what's
# about to run and prompts for confirmation before executing.

set -e

cd "$(dirname "$0")/.."

trap 'rc=$?; printf "\n[tunnel-config] aborted (exit %d) on line %d\n" "$rc" "$LINENO" >&2; exit $rc' ERR

# ---------------------------------------------------------------------------
# Colors (mirror tunnel-status.sh — degrade gracefully).
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_GREEN="$(tput setaf 2)"; C_YELLOW="$(tput setaf 3)"; C_RED="$(tput setaf 1)"
  C_BLUE="$(tput setaf 4)"; C_BOLD="$(tput bold)"; C_DIM="$(tput dim 2>/dev/null || echo '')"
  C_RESET="$(tput sgr0)"
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_BOLD=""; C_DIM=""; C_RESET=""
fi

step()  { printf "\n%s==>%s %s%s%s\n" "$C_BLUE" "$C_RESET" "$C_BOLD" "$*" "$C_RESET"; }
ok()    { printf "  %s✓%s %s\n" "$C_GREEN"  "$C_RESET" "$*"; }
warn()  { printf "  %s⚠%s %s\n" "$C_YELLOW" "$C_RESET" "$*"; }
bad()   { printf "  %s✗%s %s\n" "$C_RED"    "$C_RESET" "$*"; }
info()  { printf "  %s·%s %s\n" "$C_DIM"    "$C_RESET" "$*"; }

# confirm "prompt" — default No. Returns 0 on yes, 1 on anything else.
confirm() {
  local reply
  # /dev/tty so this works even if stdin is piped in.
  read -r -p "$1 [y/N] " reply </dev/tty || reply=""
  case "$reply" in
    y|Y|yes|YES) return 0 ;;
    *)           return 1 ;;
  esac
}

# echo + confirm + run. Aborts the wizard if the user declines.
run_step() {
  printf "    %s$ %s%s\n" "$C_DIM" "$*" "$C_RESET"
  if ! confirm "    Run this?"; then
    bad "declined — aborting wizard. Re-run when ready."
    exit 1
  fi
  "$@"
}

CFD_DIR="$HOME/.cloudflared"
CERT_PATH="$CFD_DIR/cert.pem"
CONFIG_PATH="$CFD_DIR/config.yml"
UUID_RE='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

printf "%sCloudflare named-tunnel setup wizard%s\n" "$C_BOLD" "$C_RESET"
printf "%sthis wizard is idempotent — safe to re-run.%s\n" "$C_DIM" "$C_RESET"

# ---------------------------------------------------------------------------
# Gather inputs first so the rest of the wizard can run unattended.
# ---------------------------------------------------------------------------
step "Inputs"

read -r -p "  Hostname (e.g. dashboard.example.com): " HOSTNAME_INPUT </dev/tty
if [ -z "$HOSTNAME_INPUT" ]; then
  bad "hostname is required"
  exit 1
fi
# Lightweight sanity check — must look like a FQDN. No regex char classes that
# behave differently between BSD and GNU.
case "$HOSTNAME_INPUT" in
  *.*) : ;;
  *)
    bad "that does not look like a fully-qualified hostname"
    exit 1
    ;;
esac
ok "hostname: $HOSTNAME_INPUT"

read -r -p "  Tunnel name [dashboard]: " TUNNEL_NAME </dev/tty
TUNNEL_NAME="${TUNNEL_NAME:-dashboard}"
ok "tunnel name: $TUNNEL_NAME"

# ---------------------------------------------------------------------------
# 1. cloudflared install
# ---------------------------------------------------------------------------
step "1. cloudflared install"

if command -v cloudflared >/dev/null 2>&1; then
  ok "already installed at $(command -v cloudflared)"
else
  bad "cloudflared not on PATH"
  if command -v brew >/dev/null 2>&1; then
    info "Homebrew found — can install cloudflared via brew."
    if confirm "  Install now with 'brew install cloudflared'?"; then
      brew install cloudflared
      ok "installed"
    else
      bad "declined — install cloudflared manually then re-run this wizard"
      exit 1
    fi
  else
    bad "Homebrew not found. Install cloudflared by hand, then re-run."
    info "see https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 2. cloudflared tunnel login (one-time)
# ---------------------------------------------------------------------------
step "2. Cloudflare account login"

if [ -f "$CERT_PATH" ]; then
  ok "cert.pem already at $CERT_PATH — skipping login"
else
  warn "no cert.pem yet — running 'cloudflared tunnel login' (opens a browser)"
  run_step cloudflared tunnel login
  if [ ! -f "$CERT_PATH" ]; then
    bad "login did not produce $CERT_PATH — check the browser tab and retry"
    exit 1
  fi
  ok "logged in"
fi

# ---------------------------------------------------------------------------
# 3. cloudflared tunnel create <name>
# ---------------------------------------------------------------------------
step "3. Create named tunnel '$TUNNEL_NAME'"

# `cloudflared tunnel list` output looks like:
#   ID                                   NAME       CREATED            CONNECTIONS
#   abcdef01-...                          dashboard  2024-...            ...
# Match a line that starts with a UUID and has our exact tunnel name in col 2.
EXISTING_UUID=""
if CFD_LIST_OUT="$(cloudflared tunnel list 2>/dev/null)"; then
  EXISTING_UUID="$(printf "%s\n" "$CFD_LIST_OUT" \
    | awk -v n="$TUNNEL_NAME" '$2 == n {print $1; exit}' || true)"
fi

if [ -n "$EXISTING_UUID" ]; then
  ok "tunnel '$TUNNEL_NAME' already exists ($EXISTING_UUID)"
  TUNNEL_UUID="$EXISTING_UUID"
else
  warn "tunnel '$TUNNEL_NAME' does not exist yet"
  run_step cloudflared tunnel create "$TUNNEL_NAME"
  # Re-list to pull the freshly-minted UUID.
  TUNNEL_UUID="$(cloudflared tunnel list 2>/dev/null \
    | awk -v n="$TUNNEL_NAME" '$2 == n {print $1; exit}' || true)"
  if [ -z "$TUNNEL_UUID" ]; then
    bad "could not find UUID for newly-created tunnel — bailing"
    exit 1
  fi
  ok "created ($TUNNEL_UUID)"
fi

# ---------------------------------------------------------------------------
# 4. Locate credentials file
# ---------------------------------------------------------------------------
step "4. Locate credentials file"

CREDS_PATH="$CFD_DIR/${TUNNEL_UUID}.json"
if [ -f "$CREDS_PATH" ]; then
  ok "found $CREDS_PATH"
else
  # Try a broader search in case cloudflared chose a non-default location.
  CREDS_FOUND="$(find "$CFD_DIR" -maxdepth 2 -type f -name "${TUNNEL_UUID}.json" 2>/dev/null | head -n 1 || true)"
  if [ -n "$CREDS_FOUND" ]; then
    CREDS_PATH="$CREDS_FOUND"
    ok "found at $CREDS_PATH"
  else
    bad "could not locate ${TUNNEL_UUID}.json in $CFD_DIR"
    info "look for the path printed by 'cloudflared tunnel create' and re-run"
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 5. Write ~/.cloudflared/config.yml
# ---------------------------------------------------------------------------
step "5. Write config.yml"

if [ -f "$CONFIG_PATH" ]; then
  warn "$CONFIG_PATH already exists — will back up before overwriting"
  if ! confirm "  Overwrite $CONFIG_PATH?"; then
    info "skipped — using your existing config.yml"
  else
    BACKUP="$CONFIG_PATH.bak.$(date +%Y%m%d-%H%M%S)"
    cp "$CONFIG_PATH" "$BACKUP"
    ok "backed up to $BACKUP"
    WRITE_CONFIG=1
  fi
else
  WRITE_CONFIG=1
fi

if [ "${WRITE_CONFIG:-0}" -eq 1 ]; then
  mkdir -p "$CFD_DIR"
  cat > "$CONFIG_PATH" <<EOF
# Generated by scripts/tunnel-config.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
tunnel: $TUNNEL_NAME
credentials-file: $CREDS_PATH
ingress:
  - hostname: $HOSTNAME_INPUT
    service: http://localhost:8765
  - service: http_status:404
EOF
  ok "wrote $CONFIG_PATH"
fi

# ---------------------------------------------------------------------------
# 6. cloudflared tunnel route dns <name> <hostname>
# ---------------------------------------------------------------------------
step "6. Route DNS '$HOSTNAME_INPUT' → tunnel '$TUNNEL_NAME'"

# This call is idempotent server-side — Cloudflare returns 200 if the CNAME
# already exists. Echo + confirm anyway since it touches a public DNS record.
run_step cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME_INPUT"
ok "DNS route set"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
step "Done"

ok "named tunnel '$TUNNEL_NAME' → $HOSTNAME_INPUT is configured"
printf "\n  %sNext:%s\n" "$C_BOLD" "$C_RESET"
printf "    1. Start the dashboard server (in another terminal):\n"
printf "         HOST=0.0.0.0 .venv/bin/python server.py\n"
printf "    2. Bring the tunnel up:\n"
printf "         bash scripts/tunnel-up.sh\n"
printf "       (or: cloudflared tunnel run %s)\n" "$TUNNEL_NAME"
printf "    3. To make the tunnel auto-start on boot (launchd service):\n"
printf "         sudo cloudflared service install\n"
printf "\n  Verify any time with: bash scripts/tunnel-status.sh\n\n"
