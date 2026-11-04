#!/usr/bin/env bash
#
# api-commit.sh — commit staged changes via the GitHub Git Data REST API so
# the resulting commit is auto-signed by GitHub's GPG key and shows as
# "Verified" in the UI. Plain `git push` with the workflow's GITHUB_TOKEN
# produces unsigned commits; this helper uses POST /repos/.../git/{blobs,
# trees,commits} + PATCH /repos/.../git/refs/heads/<branch> instead.
#
# Shared by every data workflow that commits back to main (lthcs-daily,
# pages, daily-audit, real-estate-daily, and the rest of .github/workflows).
# A retry fix here covers all of them.
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
#   * Blob-visibility: POST git/trees sometimes returns HTTP 422
#     "tree.sha <sha> is not a valid blob" for a blob this script just
#     created (run 41259909286, 2026-11-01). That is treated as transient:
#     the missing blob is verified and re-uploaded, then the tree is
#     rebuilt and retried with backoff. The same class of lag on a tree
#     or commit object ("is not a valid tree/commit") retries the POST.
#     A genuine validation 422 (bad mode, malformed path, ...) still
#     fails on the first response.
#   * Blob upload: POST git/blobs retries transient transport failures.
#     HTTP 5xx, rate limits, and timeouts use API_RETRY_DELAYS (the same
#     schedule as the other content-creating POSTs). An empty or truncated
#     body does not: `gh` then prints "unexpected end of JSON input" (or
#     another JSON-parse error) with no HTTP status, and that used to
#     abort on the first try. usaspending-daily run 41809419544
#     (2026-11-04 09:42 UTC) died that way on data-usaspending.json; the
#     15:47 UTC rerun succeeded. Those responses use the shorter
#     BLOB_UPLOAD_RETRY_DELAYS (default 5/15/30). A real client error
#     (HTTP 400/422, malformed path, ...) still fails on the first response.
#   * Sends ONLY message/tree/parents — no author/committer/signature.
#     That is the exact condition under which GitHub auto-signs a Git Data
#     API commit: it stamps the authenticated bot identity
#     (github-actions[bot]) and signs with its own key -> "Verified".
#     Supplying a custom author (e.g. real-estate-bot) silently yields an
#     UNSIGNED commit — that was this script's original missing-signature
#     bug. Bot provenance is carried in the commit message prefix instead
#     (e.g. "lthcs: ...", "real-estate: ...").
#   * Never force-updates the ref. A rejected fast-forward retries against
#     the new tip; the ref patch sends force=false.
#
# Exit codes:
#   0   success (committed, or nothing to commit)
#   1   API failure / unrecoverable conflict / missing inputs

set -euo pipefail

MSG="${1:?commit message required as first arg}"
BRANCH="${BRANCH:-main}"
REPO="${GH_REPO:-${GITHUB_REPOSITORY:-}}"

_err_tail() {
  if [ -f "${API_LAST_ERR:-}" ] && [ -s "$API_LAST_ERR" ]; then
    tail -1 "$API_LAST_ERR" | cut -c1-200
  fi
  return 0
}

# _fail <message>: stderr line plus, on Actions, a ::error annotation so
# the daily audit quotes this failure instead of an earlier ::warning.
_fail() {
  local msg="$1" esc
  echo "api-commit: $msg" >&2
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    esc="$(printf '%s' "$msg" | python3 -c 'import sys; s=sys.stdin.read(); print(s.replace("%","%25").replace("\r","%0D").replace("\n","%0A"), end="")')"
    printf '::error title=api-commit::%s\n' "$esc"
  fi
  exit 1
}

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
# POST killed the whole script. Waits below, all env-overridable:
#   * BLOB_THROTTLE   inter-blob sleep (default 0.8s ≈ 75/min, under the cap)
#   * API_RETRY_DELAYS backoff schedule for every content-creating POST
#     on rate limits, HTTP 5xx, timeouts, and tree/commit lag. Secondary
#     limits typically clear in <=60s, so 30/90/180 covers it.
#   * BLOB_UPLOAD_RETRY_DELAYS backoff for a git/blobs POST whose body
#     was empty or truncated (no HTTP status to classify). Shorter than
#     the rate-limit schedule: this is a cut-off response, not a lockout.
#     Default 5/15/30 is four attempts (immediate, then those three sleeps).
#   * TREE_OBJECT_RETRY_DELAYS backoff for a tree 422 "not a valid blob"
#     after the missing blob has been re-uploaded (default 5/15/30/60).
#     Short on purpose: this is replication lag, not a rate-limit lockout.
BLOB_THROTTLE="${BLOB_THROTTLE:-0.8}"
API_RETRY_DELAYS="${API_RETRY_DELAYS:-30 90 180}"
BLOB_UPLOAD_RETRY_DELAYS="${BLOB_UPLOAD_RETRY_DELAYS:-5 15 30}"
TREE_OBJECT_RETRY_DELAYS="${TREE_OBJECT_RETRY_DELAYS:-5 15 30 60}"
REF_REJECT_SLEEP="${REF_REJECT_SLEEP:-2}"
REF_RATELIMIT_SLEEP="${REF_RATELIMIT_SLEEP:-60}"
API_LAST_ERR="$(mktemp)"
BLOB_SWEEP_DONE=0
BLOB_NAMED_REPAIRED=0

# _retryable <err-file>: true when the failure is worth waiting out —
# rate limits (403/429), transient server/network errors, AND 401s.
# 401 is included deliberately: GitHub's API intermittently returns
# 401 "Requires authentication" to perfectly valid tokens during brief
# auth-service blips (observed 2026-06-10 ~15:09Z — the same minute it
# 401'd a fresh Actions GITHUB_TOKEN in run 27285292084 it also 401'd
# an interactive keyring token locally, recovering within minutes, with
# githubstatus.com all-green throughout). A genuinely dead token just
# burns the bounded schedule (~5 min) before the same abort; a blip no
# longer kills a daily data commit.
#
# "not a valid tree/commit" is the same class of lag as the blob 422
# below, but for an object this process just created (the new tree, when
# posting the commit). Retrying the same body is the right fix there.
# A tree 422 "not a valid blob" is NOT handled here: the caller re-uploads
# the blob and rebuilds the tree. Other 422s (bad mode, bad path) fail fast.
# An empty or truncated blob body is also not handled here — there is no
# HTTP status on that stderr. _blob_response_glitch covers it, and only
# for POST git/blobs, on BLOB_UPLOAD_RETRY_DELAYS.
_retryable() {
  grep -qiE 'HTTP (401|403|429|5[0-9][0-9])|rate limit|timed? ?out|connection|not a valid (tree|commit)|tree does not exist|commit does not exist' "$1"
}

# GitHub blob ids are 40 lowercase hex characters. Anything else from a
# blob POST (empty stdout, "null", a fragment of JSON) is not a sha.
_valid_blob_sha() {
  printf '%s' "$1" | grep -qE '^[0-9a-f]{40}$'
}

# _blob_response_glitch <err-file>: git/blobs got no parseable body.
# True for an empty stderr (gh exited without a message) and for the
# JSON decoder errors gh prints when a 2xx body is empty or cut off:
# "unexpected end of JSON input", "unexpected EOF", or
# "invalid character '<' looking for beginning of value" (an HTML
# gateway page). A real API error is well-formed and names an HTTP
# status or a validation message; those must not match.
_blob_response_glitch() {
  if ! grep -q '[^[:space:]]' "$1" 2>/dev/null; then
    return 0
  fi
  grep -qiE 'unexpected end of JSON input|unexpected EOF|unexpected end of file|invalid character .+ looking for beginning of value|unparseable blob response|empty blob response|failed to parse JSON|error decoding response body' "$1"
}

_blob_miss_err() {
  grep -qiE 'not a valid blob|no such blob|blob does not exist' "$1"
}

_save_err() {
  if [ -f "$1" ]; then
    cat "$1" > "$API_LAST_ERR" || true
  fi
}

# SHAs named on a blob-miss line only, so a dumped tree body cannot mark
# every entry missing.
_missing_blob_shas() {
  grep -iE 'not a valid blob|no such blob|blob does not exist' "$1" \
    | grep -oiE '[0-9a-f]{40}' \
    | tr '[:upper:]' '[:lower:]' \
    | sort -u || true
}

# _api_post <git-data-endpoint>: POST stdin payload, echo .sha.
# Buffers the payload to a temp file so it can be replayed across retries
# (stdin is consumed on the first attempt).
#   0  success (.sha on stdout)
#   2  git/trees rejected a blob that may just be invisible; caller repairs
#   1  non-retryable, or the retry schedule is exhausted
# Runs in a pipeline, so it must not exit the parent shell.
#
# Two backoff schedules, picked from the error just seen:
#   * API_RETRY_DELAYS — rate limits, HTTP 5xx, timeouts, tree/commit lag.
#   * BLOB_UPLOAD_RETRY_DELAYS — POST git/blobs whose body was empty or
#     unparseable. A later validation error still stops immediately; it
#     does not keep spending the remaining blob attempts.
_api_post() {
  local endpoint="$1" tmp sha delay i schedule detail failed_once
  local -a api_delays blob_delays
  tmp="$(mktemp)"
  cat > "$tmp"
  : > "$tmp.err"
  # shellcheck disable=SC2206
  api_delays=(0 $API_RETRY_DELAYS)
  # shellcheck disable=SC2206
  blob_delays=(0 $BLOB_UPLOAD_RETRY_DELAYS)
  # Index 0 is the first attempt (no sleep). Both lists start with that
  # synthetic 0 so a switch onto the blob schedule after attempt 1 uses
  # the first configured backoff, not another immediate try. The attempt
  # already spent lines up with index 0 on either list.
  schedule="api"
  i=0
  failed_once=0
  while : ; do
    if [ "$schedule" = "blob" ]; then
      if [ "$i" -ge "${#blob_delays[@]}" ]; then
        break
      fi
      delay="${blob_delays[$i]}"
    else
      if [ "$i" -ge "${#api_delays[@]}" ]; then
        break
      fi
      delay="${api_delays[$i]}"
    fi
    # A zero delay is still a retry when a previous attempt already failed.
    # Log it so a transient failure is visible even when the backoff is 0.
    # failed_once covers an empty stderr, which [ -s ] would miss.
    if [ "$failed_once" -eq 1 ]; then
      detail="$(tail -1 "$tmp.err" 2>/dev/null | cut -c1-120)"
      if [ -z "$detail" ]; then
        detail="empty response"
      fi
      if [ "$delay" -gt 0 ]; then
        echo "api-commit:   POST $endpoint failed (${detail}); retrying in ${delay}s..." >&2
        sleep "$delay"
      else
        echo "api-commit:   POST $endpoint failed (${detail}); retrying..." >&2
      fi
    fi
    if sha="$(gh api -X POST "repos/$REPO/$endpoint" --input "$tmp" --jq '.sha' 2>"$tmp.err")"; then
      # Exit 0 with an empty or non-sha body is the same failure as a
      # JSON parse error: gh had nothing usable to print. Trees and
      # commits keep the previous "any stdout is the sha" behaviour.
      if [ "$endpoint" != "git/blobs" ] || _valid_blob_sha "$sha"; then
        rm -f "$tmp" "$tmp.err"
        printf '%s' "$sha"
        return 0
      fi
      printf 'unparseable blob response: unexpected end of JSON input (sha=%s)\n' \
        "$(printf '%s' "$sha" | cut -c1-80)" > "$tmp.err"
    fi
    failed_once=1
    if [ "$endpoint" = "git/blobs" ] && _blob_response_glitch "$tmp.err"; then
      if ! grep -q '[^[:space:]]' "$tmp.err" 2>/dev/null; then
        printf 'empty blob response\n' > "$tmp.err"
      fi
      _save_err "$tmp.err"
      schedule="blob"
      i=$((i + 1))
      continue
    fi
    _save_err "$tmp.err"
    if [ "$endpoint" = "git/trees" ] && _blob_miss_err "$tmp.err"; then
      rm -f "$tmp" "$tmp.err"
      return 2
    fi
    if _retryable "$tmp.err"; then
      schedule="api"
      i=$((i + 1))
      continue
    fi
    break
  done
  echo "api-commit: POST $endpoint failed (non-retryable or retries exhausted):" >&2
  tail -3 "$tmp.err" >&2 || true
  _save_err "$tmp.err"
  rm -f "$tmp" "$tmp.err"
  return 1
}

_blob_payload() {
  python3 -c '
import base64, json, sys
with open(sys.argv[1], "rb") as fh:
    sys.stdout.write(json.dumps({"content": base64.b64encode(fh.read()).decode("ascii"), "encoding": "base64"}))
' "$1"
}

# Print the blob sha. Non-zero means the upload failed; the caller _fails.
# Does not exit the shell: command substitution would swallow `exit`.
_upload_one_blob() {
  local path="$1" sha rc
  set +e
  sha="$(_blob_payload "$path" | _api_post git/blobs)"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    return "$rc"
  fi
  if ! printf '%s' "$sha" | grep -qE '^[0-9a-f]{40}$'; then
    echo "api-commit: blob upload for $path returned unexpected sha '${sha}'" >&2
    return 1
  fi
  printf '%s' "$sha"
}

# Best-effort existence check. Prints visible / missing / inconclusive.
# A 403/429 during the check is not evidence the blob is gone.
_blob_verify() {
  local sha="$1" err
  err="$(mktemp)"
  if gh api "repos/$REPO/git/blobs/$sha" --jq '.sha' >/dev/null 2>"$err"; then
    rm -f "$err"
    printf 'visible'
    return 0
  fi
  if grep -qiE 'HTTP (403|429)|rate limit' "$err"; then
    rm -f "$err"
    printf 'inconclusive'
    return 0
  fi
  rm -f "$err"
  printf 'missing'
  return 0
}

_reupload_all_blobs() {
  local idx path newsha
  for idx in "${!UPSERT_PATHS[@]}"; do
    path="${UPSERT_PATHS[$idx]}"
    echo "api-commit:   re-uploading $path" >&2
    newsha="$(_upload_one_blob "$path")" || _fail "re-upload of $path failed: $(_err_tail)"
    if [ "$newsha" != "${UPSERT_SHAS[$idx]}" ]; then
      echo "api-commit:   blob SHA for $path changed ${UPSERT_SHAS[$idx]} -> $newsha" >&2
    fi
    UPSERT_SHAS[$idx]="$newsha"
    echo "  blob $newsha  $path (re-upload)"
    sleep "$BLOB_THROTTLE"
  done
}

# Re-create the blob(s) named by the last tree 422. Unknown SHAs are logged
# and left for the tree retry (replication lag on an object we do not own).
# An error that names no SHA re-uploads every staged blob once.
_repair_blob_miss() {
  local -a missing=()
  local sha idx found any_known path newsha visible
  any_known=0
  mapfile -t missing < <(_missing_blob_shas "$API_LAST_ERR")
  if [ "${#missing[@]}" -eq 0 ]; then
    echo "api-commit:   tree 422 named no blob SHA; re-uploading every staged blob" >&2
    BLOB_SWEEP_DONE=1
    _reupload_all_blobs
    return 0
  fi
  for sha in "${missing[@]}"; do
    found=0
    for idx in "${!UPSERT_SHAS[@]}"; do
      if [ "${UPSERT_SHAS[$idx]}" = "$sha" ]; then
        found=1
        any_known=1
        path="${UPSERT_PATHS[$idx]}"
        visible="$(_blob_verify "$sha")"
        echo "api-commit:   blob $sha ($path) verify=$visible; re-uploading" >&2
        newsha="$(_upload_one_blob "$path")" || _fail "re-upload of $path failed: $(_err_tail)"
        if [ "$newsha" != "$sha" ]; then
          echo "api-commit:   blob SHA for $path changed $sha -> $newsha" >&2
        fi
        UPSERT_SHAS[$idx]="$newsha"
        echo "  blob $newsha  $path (re-upload)"
        sleep "$BLOB_THROTTLE"
      fi
    done
    if [ "$found" -eq 0 ]; then
      echo "api-commit:   missing blob $sha is not a staged upload; retrying tree after backoff" >&2
    fi
  done
  if [ "$any_known" -eq 1 ]; then
    BLOB_NAMED_REPAIRED=1
  fi
}

_build_tree_json() {
  local base_tree="$1" tree_json first path path_json idx
  tree_json='{"base_tree":"'"$base_tree"'","tree":['
  first=1
  for idx in "${!UPSERT_PATHS[@]}"; do
    [ "$first" -eq 1 ] || tree_json+=','
    first=0
    path_json=$(printf '%s' "${UPSERT_PATHS[$idx]}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    tree_json+='{"path":'"$path_json"',"mode":"100644","type":"blob","sha":"'"${UPSERT_SHAS[$idx]}"'"}'
  done
  for path in "${DELETE_PATHS[@]}"; do
    [ "$first" -eq 1 ] || tree_json+=','
    first=0
    path_json=$(printf '%s' "$path" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    tree_json+='{"path":'"$path_json"',"mode":"100644","type":"blob","sha":null}'
  done
  tree_json+=']}'
  printf '%s' "$tree_json"
}

# Create the tree, repairing invisible blobs between attempts.
# Sets NEW_TREE. Exits the script on a persistent or permanent failure.
_create_tree() {
  local base_tree="$1" delay rc tree_json blob_misses i last
  local -a delays=()
  blob_misses=0
  # shellcheck disable=SC2206
  delays=(0 $TREE_OBJECT_RETRY_DELAYS)
  last=$(( ${#delays[@]} - 1 ))
  for i in "${!delays[@]}"; do
    delay="${delays[$i]}"
    if [ "$i" -gt 0 ]; then
      if [ "$delay" -gt 0 ]; then
        echo "api-commit:   retrying tree creation in ${delay}s (blob visibility)..." >&2
        sleep "$delay"
      else
        echo "api-commit:   retrying tree creation (blob visibility)..." >&2
      fi
    fi
    tree_json="$(_build_tree_json "$base_tree")"
    set +e
    NEW_TREE="$(printf '%s' "$tree_json" | _api_post git/trees)"
    rc=$?
    set -e
    if [ "$rc" -eq 0 ]; then
      return 0
    fi
    if [ "$rc" -ne 2 ]; then
      _fail "POST git/trees failed (non-retryable or retries exhausted): $(_err_tail)"
    fi
    blob_misses=$((blob_misses + 1))
    echo "api-commit:   POST git/trees rejected a blob ($(_err_tail))" >&2
    if [ "$i" -eq "$last" ]; then
      _fail "POST git/trees failed after ${#delays[@]} attempts; blob still not visible: $(_err_tail)"
    fi
    _repair_blob_miss
    if [ "$blob_misses" -ge 2 ] && [ "$BLOB_SWEEP_DONE" -eq 0 ] && [ "$BLOB_NAMED_REPAIRED" -eq 1 ]; then
      if [ -n "$(_missing_blob_shas "$API_LAST_ERR" | head -1)" ]; then
        echo "api-commit:   blob 422 persisted after re-upload; re-uploading every staged blob once" >&2
        BLOB_SWEEP_DONE=1
        _reupload_all_blobs
      fi
    fi
  done
  _fail "POST git/trees failed: blob visibility retries exhausted: $(_err_tail)"
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
  sha="$(_upload_one_blob "$path")" || _fail "blob upload failed for $path: $(_err_tail)"
  UPSERT_SHAS+=("$sha")
  echo "  blob $sha  $path"
  sleep "$BLOB_THROTTLE"
done

attempt=0
while : ; do
  attempt=$((attempt+1))
  if (( attempt > 3 )); then
    _fail "3 attempts failed against moving ref; giving up."
  fi

  # Fresh remote tip every retry.
  base_sha="$(gh api "repos/$REPO/git/ref/heads/$BRANCH" --jq '.object.sha')"
  base_tree="$(gh api "repos/$REPO/git/commits/$base_sha" --jq '.tree.sha')"
  echo "api-commit: attempt $attempt — base $base_sha (tree $base_tree)"

  # One entry per changed path, mode 100644. Deletes send "sha": null so
  # GitHub drops the path. Everything else in $base_tree is inherited.
  _create_tree "$base_tree"
  echo "api-commit:   new tree $NEW_TREE"

  # Create the commit with ONLY message/tree/parents — deliberately no
  # author, committer, or signature. That is the exact condition under which
  # GitHub auto-signs a Git Data API commit: it stamps the authenticated bot
  # identity (github-actions[bot]) and signs with its own key, producing the
  # Verified badge. Adding an author block (as this script used to) silently
  # produced an UNSIGNED commit — that was the rollout's missing-signature bug.
  # The dynamic fields must be a *prefix* env assignment so they land in
  # os.environ. Placed AFTER `python3 -c '...'` the shell would treat them as
  # script argv (sys.argv), not env vars — so os.environ["MSG"] would KeyError.
  commit_payload=$(MSG="$MSG" NEW_TREE="$NEW_TREE" BASE_SHA="$base_sha" \
    python3 -c '
import json, os
print(json.dumps({
  "message": os.environ["MSG"],
  "tree": os.environ["NEW_TREE"],
  "parents": [os.environ["BASE_SHA"]],
}))
')

  set +e
  new_commit="$(printf '%s' "$commit_payload" | _api_post git/commits)"
  rc=$?
  set -e
  if [ "$rc" -ne 0 ]; then
    _fail "POST git/commits failed: $(_err_tail)"
  fi
  echo "api-commit:   new commit $new_commit"

  # Fast-forward update only — if main moved under us between
  # base-sha read and ref update, this returns 422 and we retry. The
  # ref update is never forced; that would clobber concurrent pushes.
  # The PATCH is also a content-modifying call, fired exactly when the
  # secondary-rate-limit budget is most depleted (right after the blob
  # loop) — so distinguish a rate-limit 403 (back off 60s; the lockout
  # window is >=60s, a 2s retry would just burn the attempts) from a real
  # fast-forward conflict (quick retry against the fresh tip).
  patch_err="$(mktemp)"
  if gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" \
      -f sha="$new_commit" -F force=false >/dev/null 2>"$patch_err"; then
    rm -f "$patch_err" "$API_LAST_ERR"
    echo "api-commit: ref updated to $new_commit (signed by GitHub)"
    echo "api-commit: verify with: gh api repos/$REPO/commits/$new_commit --jq '.commit.verification'"
    exit 0
  fi
  _save_err "$patch_err"
  if _retryable "$patch_err"; then
    echo "api-commit: ref update rate-limited ($(tail -1 "$patch_err" | cut -c1-120)); backing off ${REF_RATELIMIT_SLEEP}s..." >&2
    rm -f "$patch_err"
    sleep "$REF_RATELIMIT_SLEEP"
  else
    echo "api-commit: ref update rejected ($(tail -1 "$patch_err" | cut -c1-120)); retrying against fresh tip..." >&2
    rm -f "$patch_err"
    sleep "$REF_REJECT_SLEEP"
  fi
done
