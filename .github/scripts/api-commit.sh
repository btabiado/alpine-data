#!/usr/bin/env bash
#
# api-commit.sh — commit staged changes via the GitHub Git Data REST API so
# the resulting commit is auto-signed by GitHub's GPG key and shows as
# "Verified" in the UI. Plain `git push` with the workflow's GITHUB_TOKEN
# produces unsigned commits; this helper uses POST /repos/.../git/{blobs,
# trees,commits} + PATCH /repos/.../git/refs/heads/<branch> instead.
#
# Inputs (env / args):
#   $1                  commit message (required, single line OK)
#   BRANCH              target branch (default: main)
#   GH_REPO / GITHUB_REPOSITORY   owner/repo (auto in Actions)
#   GH_TOKEN / GITHUB_TOKEN       PAT/Actions token with contents:write
#
# Caller contract:
#   * Must have already staged the intended changes with `git add` (this
#     script reads the staged index via `git diff --cached --name-status`
#     so adds, modifies, AND deletes all round-trip correctly).
#   * Working tree should be clean otherwise; unstaged changes are ignored.
#
# Behaviour:
#   * No-op (exit 0) if the staged diff is empty.
#   * Race-safe: re-reads remote HEAD between retries, rebuilds tree
#     against the new base, retries up to 3 times on ref-update conflicts.
#   * Sends ONLY message/tree/parents — no author/committer/signature.
#     That is the exact condition under which GitHub auto-signs a Git Data
#     API commit: it stamps the authenticated bot identity
#     (github-actions[bot]) and signs with its own key -> "Verified".
#     Supplying a custom author (e.g. real-estate-bot) silently yields an
#     UNSIGNED commit — that was this script's original missing-signature
#     bug. Bot provenance is carried in the commit message prefix instead
#     (e.g. "lthcs: ...", "real-estate: ...").
#
# Exit codes:
#   0   success (committed, or nothing to commit)
#   1   API failure / unrecoverable conflict / missing inputs

set -euo pipefail

MSG="${1:?commit message required as first arg}"
BRANCH="${BRANCH:-main}"
REPO="${GH_REPO:-${GITHUB_REPOSITORY:-}}"

if [ -z "$REPO" ]; then
  echo "api-commit: GITHUB_REPOSITORY / GH_REPO unset; aborting." >&2
  exit 1
fi
if [ -z "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]; then
  echo "api-commit: GH_TOKEN / GITHUB_TOKEN unset; aborting." >&2
  exit 1
fi
export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"

# Bail early if nothing is staged.
if git diff --cached --quiet; then
  echo "api-commit: no staged changes; skipping."
  exit 0
fi

# Build the change list once — paths and statuses don't change between
# retries (we always commit the same logical diff, just against a moving
# parent). Use NUL separation so paths with spaces are safe.
mapfile -d '' STAGED_RAW < <(git diff --cached --name-status -z)

# Parse name-status into three parallel arrays. -z output is:
#   <status>\0<path>\0   for A/M/D
#   R<score>\0<old>\0<new>\0   for renames (treat as delete-old + add-new)
declare -a UPSERT_PATHS=()    # add or modify — need blob upload
declare -a DELETE_PATHS=()    # mark sha=null in tree
i=0
n=${#STAGED_RAW[@]}
while (( i < n )); do
  status="${STAGED_RAW[i]}"; i=$((i+1))
  case "$status" in
    A|M)
      UPSERT_PATHS+=("${STAGED_RAW[i]}"); i=$((i+1))
      ;;
    D)
      DELETE_PATHS+=("${STAGED_RAW[i]}"); i=$((i+1))
      ;;
    R*)
      # Rename: drop old path, add new path with current content.
      DELETE_PATHS+=("${STAGED_RAW[i]}"); i=$((i+1))
      UPSERT_PATHS+=("${STAGED_RAW[i]}"); i=$((i+1))
      ;;
    C*)
      # Copy: just add the new path.
      i=$((i+1))  # skip source
      UPSERT_PATHS+=("${STAGED_RAW[i]}"); i=$((i+1))
      ;;
    *)
      echo "api-commit: unsupported status '$status'; aborting." >&2
      exit 1
      ;;
  esac
done

echo "api-commit: ${#UPSERT_PATHS[@]} upsert, ${#DELETE_PATHS[@]} delete on $REPO@$BRANCH"

# Rate-limit hardening. GitHub's SECONDARY rate limit caps content-generating
# POSTs (~80/min per its abuse guidance); the old back-to-back blob loop ran
# ~240/min on the ~170-file lthcs history commit and tripped HTTP 403
# "secondary rate limit" daily (run 27243888112) — and with set -e, ONE failed
# POST killed the whole script. Two defenses:
#   * BLOB_THROTTLE   inter-blob sleep (default 0.8s ≈ 75/min, under the cap)
#   * API_RETRY_DELAYS backoff schedule for every content-creating POST;
#     secondary limits typically clear in <=60s, so 30/90/180 covers it.
# Both env-overridable (handy for local harnesses / CI debugging).
BLOB_THROTTLE="${BLOB_THROTTLE:-0.8}"
API_RETRY_DELAYS="${API_RETRY_DELAYS:-30 90 180}"

# _retryable <err-file>: true when the failure is worth waiting out —
# rate limits (403/429), transient server/network errors, AND 401s.
# 401 is included deliberately: GitHub's API intermittently returns
# 401 "Requires authentication" to perfectly valid tokens during brief
# auth-service blips (observed 2026-06-10 ~15:09Z — the same minute it
# 401'd a fresh Actions GITHUB_TOKEN in run 27285292084 it also 401'd
# an interactive keyring token locally, recovering within minutes, with
# githubstatus.com all-green throughout). A genuinely dead token just
# burns the bounded schedule (~5 min) before the same abort; a blip no
# longer kills a daily data commit. Truly permanent errors (404, 422
# validation) still fail fast.
_retryable() {
  grep -qiE 'HTTP (401|403|429|5[0-9][0-9])|rate limit|timed? ?out|connection' "$1"
}

# _api_post <git-data-endpoint>: POST stdin payload, echo .sha.
# Buffers the payload to a temp file so it can be replayed across retries
# (stdin is consumed on the first attempt). Returns 1 on a non-retryable
# error or once the schedule is exhausted; callers run under set -e so
# that still aborts the script, but only after backoff instead of on the
# first 403.
_api_post() {
  local endpoint="$1" tmp sha delay
  tmp="$(mktemp)"
  cat > "$tmp"
  : > "$tmp.err"
  for delay in 0 $API_RETRY_DELAYS; do
    if [ "$delay" -gt 0 ]; then
      echo "api-commit:   POST $endpoint failed ($(tail -1 "$tmp.err" 2>/dev/null | cut -c1-120)); retrying in ${delay}s..." >&2
      sleep "$delay"
    fi
    if sha="$(gh api -X POST "repos/$REPO/$endpoint" --input "$tmp" --jq '.sha' 2>"$tmp.err")"; then
      rm -f "$tmp" "$tmp.err"
      printf '%s' "$sha"
      return 0
    fi
    _retryable "$tmp.err" || break
  done
  echo "api-commit: POST $endpoint failed (non-retryable or retries exhausted):" >&2
  tail -3 "$tmp.err" >&2 || true
  rm -f "$tmp" "$tmp.err"
  return 1
}

# Upload blobs once (content-addressed — re-uploading the same blob across
# retries is harmless and returns the same SHA).
declare -a UPSERT_SHAS=()
for path in "${UPSERT_PATHS[@]}"; do
  # Build the blob payload in python3 (read file -> base64 -> JSON) and stream
  # it to the API over stdin. The old code base64-encoded into a shell var and
  # passed it as `-f content=...`; that blows past ARG_MAX on large data files
  # (the ~3.4MB data/real_estate.json -> ~4.6MB arg -> "Argument list too long",
  # exit 126, gh never runs). Piping via --input - has no size limit and matches
  # the tree/commit POSTs below. python3 is already required by this script.
  sha="$(python3 -c '
import base64, json, sys
with open(sys.argv[1], "rb") as fh:
    sys.stdout.write(json.dumps({"content": base64.b64encode(fh.read()).decode("ascii"), "encoding": "base64"}))
' "$path" | _api_post git/blobs)"
  UPSERT_SHAS+=("$sha")
  echo "  blob $sha  $path"
  sleep "$BLOB_THROTTLE"
done

attempt=0
while : ; do
  attempt=$((attempt+1))
  if (( attempt > 3 )); then
    echo "api-commit: 3 attempts failed against moving ref; giving up." >&2
    exit 1
  fi

  # Fresh remote tip every retry.
  base_sha="$(gh api "repos/$REPO/git/ref/heads/$BRANCH" --jq '.object.sha')"
  base_tree="$(gh api "repos/$REPO/git/commits/$base_sha" --jq '.tree.sha')"
  echo "api-commit: attempt $attempt — base $base_sha (tree $base_tree)"

  # Build the tree-edit payload: one entry per changed path, all relative
  # to the repo root, mode 100644 (regular file). For deletes we send
  # "sha": null which tells GitHub to drop the path from the new tree.
  # The remaining files in $base_tree are inherited unchanged.
  tree_json='{"base_tree":"'"$base_tree"'","tree":['
  first=1
  for idx in "${!UPSERT_PATHS[@]}"; do
    [ $first -eq 1 ] || tree_json+=','
    first=0
    path_json=$(printf '%s' "${UPSERT_PATHS[$idx]}" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')
    tree_json+='{"path":'"$path_json"',"mode":"100644","type":"blob","sha":"'"${UPSERT_SHAS[$idx]}"'"}'
  done
  for path in "${DELETE_PATHS[@]}"; do
    [ $first -eq 1 ] || tree_json+=','
    first=0
    path_json=$(printf '%s' "$path" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')
    tree_json+='{"path":'"$path_json"',"mode":"100644","type":"blob","sha":null}'
  done
  tree_json+=']}'

  new_tree="$(printf '%s' "$tree_json" | _api_post git/trees)"
  echo "api-commit:   new tree $new_tree"

  # Create the commit with ONLY message/tree/parents — deliberately no
  # author, committer, or signature. That is the exact condition under which
  # GitHub auto-signs a Git Data API commit: it stamps the authenticated bot
  # identity (github-actions[bot]) and signs with its own key, producing the
  # Verified badge. Adding an author block (as this script used to) silently
  # produced an UNSIGNED commit — that was the rollout's missing-signature bug.
  # The dynamic fields must be a *prefix* env assignment so they land in
  # os.environ. Placed AFTER `python3 -c '...'` the shell would treat them as
  # script argv (sys.argv), not env vars — so os.environ["MSG"] would KeyError.
  commit_payload=$(MSG="$MSG" NEW_TREE="$new_tree" BASE_SHA="$base_sha" \
    python3 -c '
import json, os
print(json.dumps({
  "message": os.environ["MSG"],
  "tree": os.environ["NEW_TREE"],
  "parents": [os.environ["BASE_SHA"]],
}))
')

  new_commit="$(printf '%s' "$commit_payload" | _api_post git/commits)"
  echo "api-commit:   new commit $new_commit"

  # Fast-forward update only — if main moved under us between
  # base-sha read and ref update, this returns 422 and we retry. We
  # intentionally DO NOT use force=true; that would clobber concurrent
  # pushes from other bots / devs.
  # The PATCH is also a content-modifying call, fired exactly when the
  # secondary-rate-limit budget is most depleted (right after the blob
  # loop) — so distinguish a rate-limit 403 (back off 60s; the lockout
  # window is >=60s, a 2s retry would just burn the attempts) from a real
  # fast-forward conflict (quick retry against the fresh tip).
  patch_err="$(mktemp)"
  if gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" \
      -f sha="$new_commit" -F force=false >/dev/null 2>"$patch_err"; then
    rm -f "$patch_err"
    echo "api-commit: ref updated to $new_commit (signed by GitHub)"
    echo "api-commit: verify with: gh api repos/$REPO/commits/$new_commit --jq '.commit.verification'"
    exit 0
  fi
  if _retryable "$patch_err"; then
    echo "api-commit: ref update rate-limited ($(tail -1 "$patch_err" | cut -c1-120)); backing off 60s..." >&2
    rm -f "$patch_err"
    sleep 60
  else
    echo "api-commit: ref update rejected ($(tail -1 "$patch_err" | cut -c1-120)); retrying against fresh tip..." >&2
    rm -f "$patch_err"
    sleep 2
  fi
done
