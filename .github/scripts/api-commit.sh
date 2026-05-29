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
#   AUTHOR_NAME         commit author display name (default: github-actions[bot])
#   AUTHOR_EMAIL        commit author email (default: 41898282+github-actions[bot]@users.noreply.github.com)
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
#   * Author email defaults to the github-actions[bot] noreply so the
#     signed commit's author lines up with the signer; pass
#     AUTHOR_EMAIL=lthcs-bot@users.noreply.github.com explicitly if you
#     need the historical bot identity preserved (commit will still be
#     signed/verified — GitHub signs based on the token, not the author).
#
# Exit codes:
#   0   success (committed, or nothing to commit)
#   1   API failure / unrecoverable conflict / missing inputs

set -euo pipefail

MSG="${1:?commit message required as first arg}"
BRANCH="${BRANCH:-main}"
REPO="${GH_REPO:-${GITHUB_REPOSITORY:-}}"
AUTHOR_NAME="${AUTHOR_NAME:-github-actions[bot]}"
AUTHOR_EMAIL="${AUTHOR_EMAIL:-41898282+github-actions[bot]@users.noreply.github.com}"

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
' "$path" | gh api -X POST "repos/$REPO/git/blobs" --input - --jq '.sha')"
  UPSERT_SHAS+=("$sha")
  echo "  blob $sha  $path"
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

  new_tree="$(printf '%s' "$tree_json" | gh api -X POST "repos/$REPO/git/trees" --input - --jq '.sha')"
  echo "api-commit:   new tree $new_tree"

  # Create the signed commit. Author block (name+email+date) is preserved
  # verbatim; GitHub signs the commit object with its internal key when
  # the request is authenticated via GITHUB_TOKEN, producing the
  # Verified badge.
  commit_payload=$(python3 -c '
import json, os, sys
print(json.dumps({
  "message": os.environ["MSG"],
  "tree": os.environ["NEW_TREE"],
  "parents": [os.environ["BASE_SHA"]],
  "author": {
    "name": os.environ["AUTHOR_NAME"],
    "email": os.environ["AUTHOR_EMAIL"],
  },
}))
' MSG="$MSG" NEW_TREE="$new_tree" BASE_SHA="$base_sha" \
   AUTHOR_NAME="$AUTHOR_NAME" AUTHOR_EMAIL="$AUTHOR_EMAIL")

  new_commit="$(printf '%s' "$commit_payload" | gh api -X POST "repos/$REPO/git/commits" --input - --jq '.sha')"
  echo "api-commit:   new commit $new_commit"

  # Fast-forward update only — if main moved under us between
  # base-sha read and ref update, this returns 422 and we retry. We
  # intentionally DO NOT use force=true; that would clobber concurrent
  # pushes from other bots / devs.
  if gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" \
      -f sha="$new_commit" -F force=false >/dev/null 2>&1; then
    echo "api-commit: ref updated to $new_commit (signed by GitHub)"
    echo "api-commit: verify with: gh api repos/$REPO/commits/$new_commit --jq '.commit.verification'"
    exit 0
  fi
  echo "api-commit: ref update rejected (likely fast-forward conflict); retrying..."
  sleep 2
done
