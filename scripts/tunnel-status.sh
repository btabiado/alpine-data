#!/usr/bin/env bash
# tunnel-status.sh — diagnose where you are in named-tunnel setup.
#
# Probes cloudflared, login state, configured tunnels, DNS routes, and the
# local dashboard server, then prints a single "do this next" recommendation.
# Safe to run repeatedly. Read-only — does not modify any state.

set -e

# Anchor to repo root no matter where the script was invoked from.
cd "$(dirname "$0")/.."

# Print a useful message on unexpected failure instead of a bare "command failed".
trap 'rc=$?; printf "\n[tunnel-status] aborted (exit %d) on line %d\n" "$rc" "$LINENO" >&2; exit $rc' ERR

# ---------------------------------------------------------------------------
# Color helpers (degrade gracefully if tput is unavailable or stdout is not a tty).
# ---------------------------------------------------------------------------
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
  C_GREEN="$(tput setaf 2)"
  C_YELLOW="$(tput setaf 3)"
  C_RED="$(tput setaf 1)"
  C_BLUE="$(tput setaf 4)"
  C_BOLD="$(tput bold)"
  C_DIM="$(tput dim 2>/dev/null || echo '')"
  C_RESET="$(tput sgr0)"
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_BLUE=""; C_BOLD=""; C_DIM=""; C_RESET=""
fi

ok()   { printf "  %s✓%s %s\n" "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf "  %s⚠%s %s\n" "$C_YELLOW" "$C_RESET" "$*"; }
bad()  { printf "  %s✗%s %s\n" "$C_RED"    "$C_RESET" "$*"; }
info() { printf "  %s·%s %s\n" "$C_DIM"    "$C_RESET" "$*"; }

section() {
  printf "\n%s%s%s\n" "$C_BOLD" "$1" "$C_RESET"
}

printf "%sCloudflare named-tunnel status%s\n" "$C_BOLD" "$C_RESET"
printf "%srepo: %s%s\n" "$C_DIM" "$(pwd)" "$C_RESET"

# ---------------------------------------------------------------------------
# 1. cloudflared on PATH?
# ---------------------------------------------------------------------------
section "1. cloudflared binary"

CFD_INSTALLED=0
if command -v cloudflared >/dev/null 2>&1; then
  CFD_PATH="$(command -v cloudflared)"
  # `cloudflared --version` writes to stdout; tolerate odd output gracefully.
  CFD_VERSION="$(cloudflared --version 2>/dev/null | head -n 1 || true)"
  ok "installed at $CFD_PATH"
  [ -n "$CFD_VERSION" ] && info "$CFD_VERSION"
  CFD_INSTALLED=1
else
  bad "not on PATH"
fi

# ---------------------------------------------------------------------------
# 2. Cloudflare login (cert.pem present?)
# ---------------------------------------------------------------------------
section "2. Cloudflare account login"

CFD_DIR="$HOME/.cloudflared"
CERT_PATH="$CFD_DIR/cert.pem"
LOGGED_IN=0
if [ -f "$CERT_PATH" ]; then
  ok "cert.pem found at $CERT_PATH"
  LOGGED_IN=1
else
  bad "no cert.pem at $CERT_PATH"
  info "you have not run 'cloudflared tunnel login' yet"
fi

# ---------------------------------------------------------------------------
# 3. Named tunnels (cloudflared tunnel list)
# ---------------------------------------------------------------------------
section "3. Named tunnels"

# UUID regex (case-insensitive, dashed form). Used in multiple places below.
UUID_RE='[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'

TUNNEL_NAMES=""
TUNNEL_COUNT=0
if [ "$CFD_INSTALLED" -eq 1 ] && [ "$LOGGED_IN" -eq 1 ]; then
  # 2>/dev/null per spec — tolerate transient network/CF errors silently here.
  CFD_LIST_OUT="$(cloudflared tunnel list 2>/dev/null || true)"
  if [ -n "$CFD_LIST_OUT" ]; then
    # Parse the table: keep only lines that begin with a UUID, then take the
    # 2nd whitespace-separated column (the NAME).
    TUNNEL_NAMES="$(printf "%s\n" "$CFD_LIST_OUT" \
      | grep -E "^${UUID_RE}" \
      | awk '{print $2}' || true)"
    if [ -n "$TUNNEL_NAMES" ]; then
      # Count without invoking wc directly to avoid leading whitespace surprises.
      TUNNEL_COUNT="$(printf "%s\n" "$TUNNEL_NAMES" | grep -c . || true)"
      ok "$TUNNEL_COUNT named tunnel(s) found:"
      printf "%s\n" "$TUNNEL_NAMES" | while IFS= read -r t; do
        [ -n "$t" ] && info "$t"
      done
    else
      warn "no named tunnels exist yet"
    fi
  else
    warn "could not list tunnels (network issue or no tunnels)"
  fi
elif [ "$CFD_INSTALLED" -eq 0 ]; then
  info "skipped (cloudflared not installed)"
else
  info "skipped (not logged in)"
fi

# ---------------------------------------------------------------------------
# 4. ~/.cloudflared/config.yml
# ---------------------------------------------------------------------------
section "4. config.yml"

CONFIG_PATH="$CFD_DIR/config.yml"
HAS_CONFIG=0
if [ -f "$CONFIG_PATH" ]; then
  ok "found at $CONFIG_PATH"
  HAS_CONFIG=1
  printf "    %s---%s\n" "$C_DIM" "$C_RESET"
  # Redact the credentials path (it contains the tunnel UUID which is a secret
  # only in that it can be combined with an attacker-controlled credentials
  # file; still, be conservative and avoid printing it in shareable output).
  while IFS= read -r line; do
    case "$line" in
      *credentials-file:*)
        printf "    %s%s%s\n" "$C_DIM" "credentials-file: <redacted>" "$C_RESET"
        ;;
      *)
        printf "    %s%s%s\n" "$C_DIM" "$line" "$C_RESET"
        ;;
    esac
  done < "$CONFIG_PATH"
  printf "    %s---%s\n" "$C_DIM" "$C_RESET"
else
  warn "no config.yml at $CONFIG_PATH"
fi

# ---------------------------------------------------------------------------
# 5. Dashboard server reachable on localhost:8765?
# ---------------------------------------------------------------------------
section "5. Local dashboard server (localhost:8765)"

SERVER_UP=0
# Prefer /healthz since SETUP.md says it bypasses Basic Auth.
if command -v curl >/dev/null 2>&1; then
  HEALTH_CODE="$(curl -fsS -o /dev/null -w "%{http_code}" \
    --max-time 3 http://localhost:8765/healthz 2>/dev/null || echo 000)"
  if [ "$HEALTH_CODE" = "200" ]; then
    ok "responding on /healthz (HTTP $HEALTH_CODE)"
    SERVER_UP=1
  elif [ "$HEALTH_CODE" = "000" ]; then
    bad "not responding (connection refused / timeout)"
    info "start it with: HOST=0.0.0.0 .venv/bin/python server.py"
  else
    warn "responding but with HTTP $HEALTH_CODE"
    SERVER_UP=1
  fi
else
  warn "curl not available — skipped"
fi

# ---------------------------------------------------------------------------
# 6. DNS resolution per tunnel hostname (if config.yml lists any)
# ---------------------------------------------------------------------------
section "6. Configured hostname → DNS"

if [ "$HAS_CONFIG" -eq 1 ]; then
  # Pull every "hostname: foo.example.com" line out of config.yml. Tolerant of
  # leading whitespace, dashes for list entries, quotes.
  HOSTNAMES="$(grep -E '^[[:space:]-]*hostname:' "$CONFIG_PATH" 2>/dev/null \
    | sed -E 's/^[[:space:]-]*hostname:[[:space:]]*//; s/^[\"'\'']//; s/[\"'\'']$//' \
    | sed 's/[[:space:]]*$//' || true)"
  if [ -n "$HOSTNAMES" ]; then
    printf "%s\n" "$HOSTNAMES" | while IFS= read -r host; do
      [ -z "$host" ] && continue
      # `host` is the macOS BSD-compatible resolver. Falls back to dig if missing.
      if command -v host >/dev/null 2>&1; then
        if host -W 3 "$host" >/dev/null 2>&1; then
          ok "$host resolves"
        else
          bad "$host does not resolve (DNS route not set?)"
        fi
      elif command -v dig >/dev/null 2>&1; then
        if [ -n "$(dig +short +time=3 +tries=1 "$host" 2>/dev/null)" ]; then
          ok "$host resolves"
        else
          bad "$host does not resolve (DNS route not set?)"
        fi
      else
        info "no host/dig — skipped DNS check for $host"
      fi
    done
  else
    warn "no 'hostname:' lines found in config.yml"
  fi
else
  info "skipped (no config.yml)"
fi

# ---------------------------------------------------------------------------
# 7. Adjacent env: FRED_API_KEY in shell rc?
# ---------------------------------------------------------------------------
section "7. Adjacent: macro overlay (FRED)"

# This is informational only — it's not part of the tunnel itself, but the
# spec asks us to surface it if the user wired it up.
FRED_SET=0
if [ -n "${FRED_API_KEY:-}" ]; then
  ok "FRED_API_KEY is set in the current shell — macro chart wired up"
  FRED_SET=1
elif [ -f "$HOME/.zprofile" ] && grep -q 'FRED_API_KEY' "$HOME/.zprofile" 2>/dev/null; then
  ok "FRED_API_KEY found in ~/.zprofile — macro chart wired up (source ~/.zprofile to activate)"
  FRED_SET=1
else
  info "FRED_API_KEY not set (macro overlay disabled — see docs/SETUP.md §5)"
fi

# ---------------------------------------------------------------------------
# Next-step recommendation
# ---------------------------------------------------------------------------
section "Next step"

if [ "$CFD_INSTALLED" -eq 0 ]; then
  printf "  %s→%s install cloudflared:\n" "$C_BLUE" "$C_RESET"
  printf "       brew install cloudflared\n"
  printf "    or see docs/SETUP.md §4\n"
elif [ "$LOGGED_IN" -eq 0 ]; then
  printf "  %s→%s log in to your Cloudflare account:\n" "$C_BLUE" "$C_RESET"
  printf "       cloudflared tunnel login\n"
elif [ "$TUNNEL_COUNT" -eq 0 ]; then
  printf "  %s→%s create your first named tunnel:\n" "$C_BLUE" "$C_RESET"
  printf "       cloudflared tunnel create dashboard\n"
  printf "    or run the wizard: bash scripts/tunnel-config.sh\n"
elif [ "$HAS_CONFIG" -eq 0 ]; then
  printf "  %s→%s no config.yml yet — run the wizard to write one:\n" "$C_BLUE" "$C_RESET"
  printf "       bash scripts/tunnel-config.sh\n"
else
  # Have install + login + tunnel + config. Check whether any configured
  # hostname failed DNS resolution above.
  DNS_OK=1
  if [ -n "${HOSTNAMES:-}" ]; then
    for host in $HOSTNAMES; do
      if command -v host >/dev/null 2>&1; then
        host -W 3 "$host" >/dev/null 2>&1 || DNS_OK=0
      elif command -v dig >/dev/null 2>&1; then
        [ -n "$(dig +short +time=3 +tries=1 "$host" 2>/dev/null)" ] || DNS_OK=0
      fi
    done
  fi
  # First tunnel name from the list, for the suggestion text.
  FIRST_TUNNEL="$(printf "%s\n" "$TUNNEL_NAMES" | head -n 1)"
  FIRST_TUNNEL="${FIRST_TUNNEL:-dashboard}"
  if [ "$DNS_OK" -eq 0 ]; then
    printf "  %s→%s route DNS to your tunnel:\n" "$C_BLUE" "$C_RESET"
    printf "       cloudflared tunnel route dns %s <YOUR_HOSTNAME>\n" "$FIRST_TUNNEL"
  elif [ "$SERVER_UP" -eq 0 ]; then
    printf "  %s→%s start the dashboard server first:\n" "$C_BLUE" "$C_RESET"
    printf "       HOST=0.0.0.0 .venv/bin/python server.py\n"
    printf "    then in another terminal:\n"
    printf "       cloudflared tunnel run %s\n" "$FIRST_TUNNEL"
  else
    printf "  %s✓%s everything looks good. Bring it up:\n" "$C_GREEN" "$C_RESET"
    printf "       cloudflared tunnel run %s\n" "$FIRST_TUNNEL"
    printf "    or: bash scripts/tunnel-up.sh\n"
  fi
fi

printf "\n"
