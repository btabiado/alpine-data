"""Retry behaviour of .github/scripts/api-commit.sh.

Daily audit 2026-11-01: lthcs-daily run 41259909286 died in the final
commit step with

    gh: tree.sha 778f23a6e9ac4dab07043db542c1238fa6896cc6 is not a valid blob (HTTP 422)

and the helper treated that 422 as non-retryable. The next scheduled run
succeeded. These tests drive the script against a fake ``gh`` so a
transient missing blob is re-created and the tree is retried, while a
real validation error still fails on the first response.

Daily audit 2026-11-04: usaspending-daily run 41809419544 (09:42 UTC)
died on the blob upload itself:

    unexpected end of JSON input
    blob upload failed for data-usaspending.json: unexpected end of JSON input

`gh` prints that when the response body is empty or truncated and there
is no HTTP status. The 15:47 UTC rerun (41861292970) succeeded. Blob
uploads now retry that class, and HTTP 5xx, a few times with backoff.
A 400/422 from the blob endpoint still fails on the first response.

Every data workflow calls this one script, so the retry covers them all.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "api-commit.sh"

FAKE_GH = textwrap.dedent(
    r'''
    #!/usr/bin/env python3
    """Stand-in for `gh api`. Records calls and answers from env + state."""
    import base64
    import hashlib
    import json
    import os
    import sys
    from pathlib import Path

    state_path = Path(os.environ["GH_FAKE_STATE"])
    log_path = Path(os.environ["GH_FAKE_LOG"])


    def load():
        if state_path.exists():
            return json.loads(state_path.read_text())
        return {}


    def save(state):
        state_path.write_text(json.dumps(state))


    def log(entry):
        with log_path.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")


    def die(msg, code=1):
        print(msg, file=sys.stderr)
        sys.exit(code)


    def git_blob_sha(data: bytes) -> str:
        header = f"blob {len(data)}\0".encode()
        return hashlib.sha1(header + data).hexdigest()


    def intval(name, default=0):
        raw = os.environ.get(name, "")
        if not raw:
            return default
        return int(raw)


    args = sys.argv[1:]
    if args and args[0] == "api":
        args = args[1:]

    method = "GET"
    jq = None
    input_file = None
    fields = {}
    positionals = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-X" and i + 1 < len(args):
            method = args[i + 1]
            i += 2
            continue
        if a == "--jq" and i + 1 < len(args):
            jq = args[i + 1]
            i += 2
            continue
        if a == "--input" and i + 1 < len(args):
            input_file = args[i + 1]
            i += 2
            continue
        if a in ("-f", "-F") and i + 1 < len(args):
            key, val = args[i + 1].split("=", 1)
            fields[key] = val
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        positionals.append(a)
        i += 1

    endpoint = positionals[0] if positionals else ""
    body = None
    if input_file:
        raw = Path(input_file).read_text() if input_file != "-" else sys.stdin.read()
        body = json.loads(raw) if raw else None

    state = load()
    state.setdefault("blob_posts", [])
    state.setdefault("tree_posts", [])
    state.setdefault("commit_posts", 0)
    state.setdefault("patches", [])
    state.setdefault("blob_gets", [])

    def note_and_die(kind, msg):
        log({"kind": kind, "endpoint": endpoint, "method": method, "error": msg})
        save(state)
        die(msg)

    if endpoint.endswith("/git/blobs") and method == "POST":
        content = base64.b64decode(body["content"])
        text = content.decode("utf-8", "replace")
        real = git_blob_sha(content)
        n = len(state["blob_posts"])
        if n < intval("GH_FAKE_BLOB_403"):
            state["blob_posts"].append({"text": text, "sha": None, "status": 403})
            note_and_die("blob", "gh: You have exceeded a secondary rate limit (HTTP 403)")
        if n < intval("GH_FAKE_BLOB_GLITCH"):
            msg = os.environ.get("GH_FAKE_BLOB_GLITCH_MSG") or "unexpected end of JSON input"
            state["blob_posts"].append({"text": text, "sha": None, "status": 0})
            note_and_die("blob", msg)
        if n < intval("GH_FAKE_BLOB_SILENT"):
            state["blob_posts"].append({"text": text, "sha": None, "status": 0})
            log({"kind": "blob", "endpoint": endpoint, "text": text, "sha": None, "status": 0})
            save(state)
            sys.exit(1)
        if n < intval("GH_FAKE_BLOB_EMPTY"):
            state["blob_posts"].append({"text": text, "sha": "", "status": 200})
            log({"kind": "blob", "endpoint": endpoint, "text": text, "sha": "", "status": 200})
            save(state)
            sys.exit(0)
        if n < intval("GH_FAKE_BLOB_500"):
            state["blob_posts"].append({"text": text, "sha": None, "status": 500})
            note_and_die("blob", "gh: Server Error (HTTP 500)")
        if n < intval("GH_FAKE_BLOB_422"):
            state["blob_posts"].append({"text": text, "sha": None, "status": 422})
            note_and_die("blob", "gh: Invalid request. content was not supplied. (HTTP 422)")
        if n < intval("GH_FAKE_BLOB_400"):
            state["blob_posts"].append({"text": text, "sha": None, "status": 400})
            note_and_die("blob", "gh: Bad Request (HTTP 400)")
        if intval("GH_FAKE_BAD_FIRST_BLOB") and n == 0:
            sha = "0" * 40
        else:
            sha = real
        state["blob_posts"].append({"text": text, "sha": sha, "status": 200})
        log({"kind": "blob", "endpoint": endpoint, "text": text, "sha": sha})
        save(state)
        print(sha)
        sys.exit(0)

    if "/git/blobs/" in endpoint and method == "GET":
        sha = endpoint.rsplit("/", 1)[-1]
        state["blob_gets"].append(sha)
        log({"kind": "blob-get", "sha": sha})
        save(state)
        if os.environ.get("GH_FAKE_BLOB_VISIBLE") == "1":
            print(sha)
            sys.exit(0)
        die(f"gh: Not Found (HTTP 404)")

    if endpoint.endswith("/git/trees") and method == "POST":
        entries = body.get("tree") or []
        state["tree_posts"].append(entries)
        n = len(state["tree_posts"])
        log({"kind": "tree", "n": n, "entries": entries, "base": body.get("base_tree")})
        save(state)
        if intval("GH_FAKE_TREE_PERMANENT"):
            die("gh: Tree entry mode is invalid (HTTP 422)")
        for ent in entries:
            if ent.get("sha") == "0" * 40:
                die(f"gh: tree.sha {'0' * 40} is not a valid blob (HTTP 422)")
        if n <= intval("GH_FAKE_TREE_LAG"):
            die("gh: base_tree bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb is not a valid tree (HTTP 422)")
        misses = intval("GH_FAKE_TREE_BLOB_MISSES")
        if n <= misses:
            override = os.environ.get("GH_FAKE_MISS_SHA", "")
            if override:
                cited = override
            elif os.environ.get("GH_FAKE_MISS_NO_SHA") == "1":
                die("gh: tree.sha is not a valid blob (HTTP 422)")
            else:
                idx = intval("GH_FAKE_MISS_INDEX")
                blobs = [e for e in entries if e.get("sha")]
                cited = blobs[idx]["sha"] if blobs else ("f" * 40)
            die(f"gh: tree.sha {cited} is not a valid blob (HTTP 422)")
        print("t" * 40)
        sys.exit(0)

    if endpoint.endswith("/git/commits") and method == "POST":
        state["commit_posts"] += 1
        n = state["commit_posts"]
        log({"kind": "commit", "n": n, "body": body})
        save(state)
        if n <= intval("GH_FAKE_COMMIT_LAG"):
            tree = (body or {}).get("tree", "t" * 40)
            die(f"gh: tree.sha {tree} is not a valid tree (HTTP 422)")
        print("c" * 40)
        sys.exit(0)

    if "/git/ref/heads/" in endpoint and method == "GET":
        log({"kind": "ref-get", "endpoint": endpoint})
        save(state)
        print("a" * 40)
        sys.exit(0)

    if "/git/commits/" in endpoint and method == "GET":
        log({"kind": "commit-get", "endpoint": endpoint})
        save(state)
        print("b" * 40)
        sys.exit(0)

    if "/git/refs/heads/" in endpoint and method == "PATCH":
        state["patches"].append(fields)
        log({"kind": "patch", "fields": fields})
        save(state)
        if len(state["patches"]) <= intval("GH_FAKE_PATCH_REJECTS"):
            die("gh: Update is not a fast forward (HTTP 422)")
        sys.exit(0)

    note_and_die("unknown", f"fake gh: unhandled {method} {endpoint}")
    '''
)


def _init_repo(path: Path, files: dict[str, str | None]) -> None:
    """files maps path -> contents. None means delete a committed file."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    for name, text in files.items():
        if text is None:
            continue
        dest = path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _stage(path: Path, updates: dict[str, str | None]) -> None:
    for name, text in updates.items():
        dest = path / name
        if text is None:
            dest.unlink()
            subprocess.run(["git", "add", "-A", "--", name], cwd=path, check=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text)
            subprocess.run(["git", "add", "--", name], cwd=path, check=True)


def _env(tmp: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    if not gh.exists():
        # Shebang must be byte 0. dedent keeps the leading newline of the literal.
        gh.write_text(FAKE_GH.lstrip("\n"))
        gh.chmod(0o755)
        if not gh.read_text().startswith("#!"):
            raise RuntimeError("fake gh is missing its shebang")
    state = tmp / "state.json"
    log = tmp / "gh.log"
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_REPOSITORY"):
        env.pop(key, None)
    env.update({
        "PATH": f"{bin_dir}:{env.get('PATH', '')}",
        "GH_TOKEN": "test-token",
        "GH_REPO": "acme/widgets",
        "BRANCH": "main",
        "BLOB_THROTTLE": "0",
        "API_RETRY_DELAYS": "0",
        "BLOB_UPLOAD_RETRY_DELAYS": "0",
        "TREE_OBJECT_RETRY_DELAYS": "0",
        "REF_REJECT_SLEEP": "0",
        "REF_RATELIMIT_SLEEP": "0",
        "GH_FAKE_STATE": str(state),
        "GH_FAKE_LOG": str(log),
        "GITHUB_ACTIONS": "",
    })
    env.update(extra)
    return env


def _run(repo: Path, env: dict[str, str], msg: str = "lthcs: test [skip ci]"):
    return subprocess.run(
        ["bash", str(SCRIPT), msg],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _state(tmp: Path) -> dict:
    return json.loads((tmp / "state.json").read_text())


def _log(tmp: Path) -> list[dict]:
    text = (tmp / "gh.log").read_text() if (tmp / "gh.log").exists() else ""
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_script_never_force_pushes():
    text = SCRIPT.read_text()
    assert "force=true" not in text
    assert "force=false" in text


def test_workflows_share_this_helper():
    users = [
        p.name
        for p in (ROOT / ".github" / "workflows").glob("*.yml")
        if "api-commit.sh" in p.read_text()
    ]
    assert "lthcs-daily.yml" in users
    # One helper, not a copy inside the workflow that failed.
    assert len(users) >= 10


def test_transient_blob_422_reuploads_and_retries(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_TREE_BLOB_MISSES="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    state = _state(tmp_path)
    assert len(state["blob_posts"]) == 2  # initial + re-upload of the missing blob
    assert len(state["tree_posts"]) == 2
    assert state["commit_posts"] == 1
    assert state["patches"] == [{"sha": "c" * 40, "force": "false"}]
    cited = state["blob_posts"][0]["sha"]
    assert cited == state["blob_posts"][1]["sha"]
    assert f"blob {cited}" in result.stderr
    assert "verify=missing" in result.stderr
    assert "re-uploading" in result.stderr
    assert "retrying tree creation" in result.stderr
    assert "non-retryable" not in result.stderr
    assert "ref updated" in result.stdout
    # Second tree uses the same sha after the re-upload.
    assert state["tree_posts"][1][0]["sha"] == cited


def test_changed_blob_sha_is_rebuilt_into_the_tree(tmp_path: Path):
    # First upload answers with a sha GitHub then rejects. The re-upload
    # returns the real sha and the retried tree must carry that sha, not
    # the one that just 422'd.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2-changed\n"})
    env = _env(tmp_path, GH_FAKE_BAD_FIRST_BLOB="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    state = _state(tmp_path)
    assert state["blob_posts"][0]["sha"] == "0" * 40
    real = state["blob_posts"][1]["sha"]
    assert real != "0" * 40
    assert "blob SHA for data.txt changed" in result.stderr
    assert state["tree_posts"][0][0]["sha"] == "0" * 40
    assert state["tree_posts"][-1][0]["sha"] == real


def test_only_the_named_blob_is_reuploaded(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"a.txt": "alpha\n", "b.txt": "beta\n"})
    _stage(repo, {"a.txt": "alpha-2\n", "b.txt": "beta-2\n"})
    env = _env(tmp_path, GH_FAKE_TREE_BLOB_MISSES="1", GH_FAKE_MISS_INDEX="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    posts = _state(tmp_path)["blob_posts"]
    alpha = [p for p in posts if p["text"] == "alpha-2\n"]
    beta = [p for p in posts if p["text"] == "beta-2\n"]
    assert len(alpha) == 1
    assert len(beta) == 2
    assert "b.txt" in result.stderr
    assert "re-uploading every staged blob" not in result.stderr


def test_blob_422_without_a_sha_reuploads_everything(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"a.txt": "alpha\n", "b.txt": "beta\n"})
    _stage(repo, {"a.txt": "alpha-2\n", "b.txt": "beta-2\n"})
    env = _env(tmp_path, GH_FAKE_TREE_BLOB_MISSES="1", GH_FAKE_MISS_NO_SHA="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    posts = _state(tmp_path)["blob_posts"]
    assert len([p for p in posts if p["text"] == "alpha-2\n"]) == 2
    assert len([p for p in posts if p["text"] == "beta-2\n"]) == 2
    assert "named no blob SHA" in result.stderr


def test_unknown_blob_sha_retries_tree_without_reupload(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    foreign = "778f23a6e9ac4dab07043db542c1238fa6896cc6"
    env = _env(tmp_path, GH_FAKE_TREE_BLOB_MISSES="1", GH_FAKE_MISS_SHA=foreign)
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    assert len(_state(tmp_path)["blob_posts"]) == 1
    assert len(_state(tmp_path)["tree_posts"]) == 2
    assert "not a staged upload" in result.stderr
    assert foreign in result.stderr


def test_persistent_blob_422_fails_loudly_after_retries(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(
        tmp_path,
        GH_FAKE_TREE_BLOB_MISSES="99",
        TREE_OBJECT_RETRY_DELAYS="0 0",
        GITHUB_ACTIONS="true",
    )
    result = _run(repo, env)
    assert result.returncode == 1
    state = _state(tmp_path)
    assert len(state["tree_posts"]) == 3
    assert len(state["blob_posts"]) > 1
    assert state["commit_posts"] == 0
    assert state["patches"] == []
    assert "blob still not visible" in result.stderr
    assert "is not a valid blob" in result.stderr
    assert "re-uploading every staged blob once" in result.stderr
    assert "::error title=api-commit::" in result.stdout
    assert "778f23a6" not in result.stdout or "not a valid blob" in result.stdout
    # The annotation quotes the gh error, not a generic exit line.
    err_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("::error")]
    assert len(err_lines) == 1
    assert "not a valid blob" in err_lines[0]
    assert "attempt 2" not in result.stdout + result.stderr


def test_permanent_tree_422_is_not_retried(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(
        tmp_path,
        GH_FAKE_TREE_PERMANENT="1",
        TREE_OBJECT_RETRY_DELAYS="0 0 0",
        GITHUB_ACTIONS="true",
    )
    result = _run(repo, env)
    assert result.returncode == 1
    state = _state(tmp_path)
    assert len(state["tree_posts"]) == 1
    assert len(state["blob_posts"]) == 1
    assert "non-retryable" in result.stderr
    assert "mode is invalid" in result.stderr
    assert "re-uploading" not in result.stderr
    assert "retrying tree creation" not in result.stderr
    assert "::error title=api-commit::" in result.stdout
    assert "mode is invalid" in result.stdout


def test_tree_lag_retries_without_reuploading_blobs(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_TREE_LAG="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    state = _state(tmp_path)
    assert len(state["blob_posts"]) == 1
    assert len(state["tree_posts"]) == 2
    assert "not a valid tree" in result.stderr
    assert "retrying..." in result.stderr
    assert "re-uploading" not in result.stderr


def test_commit_tree_lag_retries_same_payload(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_COMMIT_LAG="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    assert _state(tmp_path)["commit_posts"] == 2
    assert "git/commits" in result.stderr
    assert "not a valid tree" in result.stderr
    assert "retrying..." in result.stderr


def test_truncated_blob_json_retries_then_commits(tmp_path: Path):
    # Audit 2026-11-04: gh's entire stderr was "unexpected end of JSON input"
    # and the helper aborted. One retry of the same payload must succeed.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data-usaspending.json": "{}\n"})
    _stage(repo, {"data-usaspending.json": '{"ok": true}\n'})
    env = _env(tmp_path, GH_FAKE_BLOB_GLITCH="1")
    result = _run(repo, env, "usaspending: daily refresh 2026-11-04 [skip ci]")
    assert result.returncode == 0, result.stderr
    posts = _state(tmp_path)["blob_posts"]
    assert [p["status"] for p in posts] == [0, 200]
    assert posts[1]["sha"]
    assert len(posts[1]["sha"]) == 40
    assert "unexpected end of JSON input" in result.stderr
    assert "retrying..." in result.stderr
    assert "non-retryable" not in result.stderr
    assert "ref updated" in result.stdout
    assert _state(tmp_path)["commit_posts"] == 1


def test_truncated_blob_json_stops_after_blob_schedule(tmp_path: Path):
    # BLOB_UPLOAD_RETRY_DELAYS="0 0" → 3 attempts. The longer API schedule
    # must not be used, or this would POST five times.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data-usaspending.json": "{}\n"})
    _stage(repo, {"data-usaspending.json": '{"ok": true}\n'})
    env = _env(
        tmp_path,
        GH_FAKE_BLOB_GLITCH="99",
        BLOB_UPLOAD_RETRY_DELAYS="0 0",
        API_RETRY_DELAYS="0 0 0 0",
        GITHUB_ACTIONS="true",
    )
    result = _run(repo, env)
    assert result.returncode == 1
    state = _state(tmp_path)
    assert len(state["blob_posts"]) == 3
    assert state["tree_posts"] == []
    assert state["commit_posts"] == 0
    assert "retries exhausted" in result.stderr
    assert "unexpected end of JSON input" in result.stderr
    err_lines = [ln for ln in result.stdout.splitlines() if ln.startswith("::error")]
    assert len(err_lines) == 1
    assert "unexpected end of JSON input" in err_lines[0]
    assert "data-usaspending.json" in err_lines[0]


def test_other_unparseable_blob_bodies_retry(tmp_path: Path):
    messages = [
        "unexpected EOF",
        "invalid character '<' looking for beginning of value",
    ]
    for i, msg in enumerate(messages):
        case = tmp_path / f"case{i}"
        case.mkdir()
        repo = case / "repo"
        repo.mkdir()
        _init_repo(repo, {"data.txt": "v1\n"})
        _stage(repo, {"data.txt": "v2\n"})
        env = _env(case, GH_FAKE_BLOB_GLITCH="1", GH_FAKE_BLOB_GLITCH_MSG=msg)
        result = _run(repo, env)
        assert result.returncode == 0, result.stderr
        assert len(_state(case)["blob_posts"]) == 2
        assert msg in result.stderr
        assert "retrying..." in result.stderr


def test_empty_stdout_blob_response_retries(tmp_path: Path):
    # gh exits 0 and prints nothing when the body decoded but had no sha.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_BLOB_EMPTY="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    posts = _state(tmp_path)["blob_posts"]
    assert len(posts) == 2
    assert posts[0]["sha"] == ""
    assert len(posts[1]["sha"]) == 40
    assert "unparseable blob response" in result.stderr
    assert "unexpected end of JSON input" in result.stderr


def test_silent_blob_failure_retries(tmp_path: Path):
    # Non-zero gh with an empty stderr is an empty response, not a
    # validation error. It must back off and try again.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_BLOB_SILENT="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    assert len(_state(tmp_path)["blob_posts"]) == 2
    assert "empty blob response" in result.stderr
    assert "retrying..." in result.stderr


def test_blob_http_500_retries_then_commits(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_BLOB_500="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    posts = _state(tmp_path)["blob_posts"]
    assert [p["status"] for p in posts] == [500, 200]
    assert "HTTP 500" in result.stderr
    assert "retrying..." in result.stderr
    assert "ref updated" in result.stdout


def test_blob_http_500_follows_api_schedule_not_blob_schedule(tmp_path: Path):
    # 5xx stays on API_RETRY_DELAYS. A nested or swapped schedule would
    # POST 5 times here (blob schedule) or 15 times (the product).
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(
        tmp_path,
        GH_FAKE_BLOB_500="99",
        API_RETRY_DELAYS="0 0",
        BLOB_UPLOAD_RETRY_DELAYS="0 0 0 0",
        GITHUB_ACTIONS="true",
    )
    result = _run(repo, env)
    assert result.returncode == 1
    assert len(_state(tmp_path)["blob_posts"]) == 3
    assert _state(tmp_path)["tree_posts"] == []
    assert "HTTP 500" in result.stderr
    assert "retries exhausted" in result.stderr
    assert "::error title=api-commit::" in result.stdout


def test_blob_validation_errors_are_not_retried(tmp_path: Path):
    # Real client errors must fail on the first response even when both
    # retry schedules still have attempts left.
    for i, (var, needle) in enumerate((
        ("GH_FAKE_BLOB_422", "HTTP 422"),
        ("GH_FAKE_BLOB_400", "HTTP 400"),
    )):
        case = tmp_path / f"case{i}"
        case.mkdir()
        repo = case / "repo"
        repo.mkdir()
        _init_repo(repo, {"data.txt": "v1\n"})
        _stage(repo, {"data.txt": "v2\n"})
        env = _env(
            case,
            **{
                var: "5",
                "BLOB_UPLOAD_RETRY_DELAYS": "0 0 0",
                "API_RETRY_DELAYS": "0 0 0",
            },
        )
        result = _run(repo, env)
        assert result.returncode == 1
        assert len(_state(case)["blob_posts"]) == 1, result.stderr
        assert needle in result.stderr
        assert "retrying" not in result.stderr
        assert _state(case)["tree_posts"] == []


def test_blob_rate_limit_still_retries(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_BLOB_403="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    posts = _state(tmp_path)["blob_posts"]
    assert [p["status"] for p in posts] == [403, 200]
    assert "retrying..." in result.stderr
    assert "rate limit" in result.stderr.lower() or "HTTP 403" in result.stderr


def test_fast_forward_rejection_retries_without_force(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    env = _env(tmp_path, GH_FAKE_PATCH_REJECTS="1")
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    patches = _state(tmp_path)["patches"]
    assert len(patches) == 2
    assert all(p.get("force") == "false" for p in patches)
    assert "retrying against fresh tip" in result.stderr
    assert "attempt 2" in result.stdout


def test_delete_is_a_null_sha_and_add_is_uploaded(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"keep.txt": "keep\n", "gone.txt": "gone\n"})
    _stage(repo, {"gone.txt": None, "new.txt": "new\n"})
    env = _env(tmp_path)
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    entries = _state(tmp_path)["tree_posts"][0]
    by_path = {e["path"]: e for e in entries}
    assert by_path["gone.txt"]["sha"] is None
    assert by_path["new.txt"]["sha"]
    assert len(_state(tmp_path)["blob_posts"]) == 1
    assert _state(tmp_path)["blob_posts"][0]["text"] == "new\n"


def test_nothing_staged_skips_the_api(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    env = _env(tmp_path)
    result = _run(repo, env)
    assert result.returncode == 0, result.stderr
    assert "no staged changes" in result.stdout
    assert not (tmp_path / "gh.log").exists()


def test_persistent_commit_lag_fails_with_annotation(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo, {"data.txt": "v1\n"})
    _stage(repo, {"data.txt": "v2\n"})
    # API_RETRY_DELAYS=0 → two attempts (the leading 0 plus this 0).
    env = _env(tmp_path, GH_FAKE_COMMIT_LAG="9", GITHUB_ACTIONS="true")
    result = _run(repo, env)
    assert result.returncode == 1
    assert _state(tmp_path)["commit_posts"] == 2
    assert "POST git/commits failed" in result.stderr
    assert "::error title=api-commit::" in result.stdout
    assert "not a valid tree" in result.stdout
