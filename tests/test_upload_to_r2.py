"""upload_to_r2.py must fail loud and must not clobber an existing object in delta mode.

The pages step used to stay green when the bucket name was unset or a PUT
failed, because this script exited 0 on purpose. Delta backfill used to fall
through to a full upload when the listing failed, which overwrites every key
under that day. Neither is acceptable.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import upload_to_r2 as u

ROOT = Path(__file__).resolve().parent.parent


class _R2Error(Exception):
    """Stand-in for botocore.exceptions.ClientError.

    The test job does not install boto3. upload_to_r2 catches whatever
    ClientError name is bound on the module, and the fixture below points
    that name here.
    """


class FakeS3:
    def __init__(self, keys=None, list_error=None, fail_uploads=()):
        self.keys = set(keys or ())
        self.list_error = list_error
        self.fail_uploads = set(fail_uploads)
        self.uploaded: list[str] = []
        self.puts: list[str] = []
        self.bodies: dict[str, bytes] = {}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        if self.list_error:
            raise self.list_error
        contents = [{"Key": k} for k in sorted(self.keys) if k.startswith(Prefix)]
        return [{"Contents": contents}]

    def upload_fileobj(self, fh, bucket, key):
        fh.read()
        if key in self.fail_uploads:
            raise _R2Error(f"upload failed: {key}")
        self.uploaded.append(key)
        self.keys.add(key)

    def put_object(self, **kwargs):
        key = kwargs["Key"]
        if key in self.fail_uploads:
            raise _R2Error(f"put failed: {key}")
        self.puts.append(key)
        self.bodies[key] = kwargs["Body"]
        self.keys.add(key)


def _client_error():
    return _R2Error("list failed")


@pytest.fixture(autouse=True)
def _stub_sdk_errors(monkeypatch):
    monkeypatch.setattr(u, "ClientError", _R2Error)
    monkeypatch.setattr(u, "BotoCoreError", _R2Error)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("R2_SKIP_IF_PRESENT", raising=False)
    monkeypatch.delenv("R2_OVERRIDE_DATE", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    (tmp_path / "data-cpi.json").write_text('{"ok": true}', encoding="utf-8")
    return tmp_path


def _patch_client(monkeypatch, fake):
    monkeypatch.setattr(
        u, "boto3",
        type("Boto3", (), {"client": staticmethod(lambda *a, **k: fake)})(),
    )
    return fake


def test_missing_bucket_is_a_failure(workspace, monkeypatch, capsys):
    monkeypatch.delenv("R2_BUCKET_NAME", raising=False)
    summary = workspace / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert u.main() == 1
    err = capsys.readouterr().out
    assert "::error title=R2 archive failed::" in err
    assert "R2_BUCKET_NAME" in summary.read_text(encoding="utf-8")


def test_bad_override_date_is_a_failure(workspace, monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "adw-warehouse")
    monkeypatch.setenv("R2_OVERRIDE_DATE", "10/07/2026")
    fake = _patch_client(monkeypatch, FakeS3())
    assert u.main() == 1
    assert fake.uploaded == [] and fake.puts == []


def test_no_payloads_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("R2_BUCKET_NAME", "adw-warehouse")
    monkeypatch.delenv("R2_OVERRIDE_DATE", raising=False)
    fake = _patch_client(monkeypatch, FakeS3())
    assert u.main() == 1
    assert fake.uploaded == []


def test_a_failed_put_fails_the_run_after_trying_the_rest(workspace, monkeypatch, capsys):
    monkeypatch.setenv("R2_BUCKET_NAME", "adw-warehouse")
    monkeypatch.setenv("R2_OVERRIDE_DATE", "2026-10-07")
    (workspace / "data-metals.json").write_text("{}", encoding="utf-8")
    fake = _patch_client(monkeypatch, FakeS3(fail_uploads={
        "raw/alpine-data/2026-10-07/data-cpi.json",
    }))
    assert u.main() == 1
    assert fake.uploaded == ["raw/alpine-data/2026-10-07/data-metals.json"]
    out = capsys.readouterr().out
    assert "uploaded 1/2 files, skipped 0" in out
    assert "data-cpi.json" in out


def test_delta_mode_does_not_overwrite_existing_objects_or_the_manifest(
        workspace, monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "adw-warehouse")
    monkeypatch.setenv("R2_OVERRIDE_DATE", "2026-10-07")
    monkeypatch.setenv("R2_SKIP_IF_PRESENT", "true")
    (workspace / "data-metals.json").write_text("{}", encoding="utf-8")
    prefix = "raw/alpine-data/2026-10-07/"
    fake = _patch_client(monkeypatch, FakeS3(keys={
        prefix + "data-cpi.json",
        prefix + "MANIFEST.json",
    }))
    assert u.main() == 0
    assert fake.uploaded == [prefix + "data-metals.json"]
    assert fake.puts == []  # existing manifest left untouched
    assert prefix + "data-cpi.json" in fake.keys


def test_delta_listing_failure_uploads_nothing(workspace, monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "adw-warehouse")
    monkeypatch.setenv("R2_OVERRIDE_DATE", "2026-10-07")
    monkeypatch.setenv("R2_SKIP_IF_PRESENT", "true")
    fake = _patch_client(monkeypatch, FakeS3(list_error=_client_error()))
    assert u.main() == 1
    assert fake.uploaded == [] and fake.puts == []


def test_daily_mode_still_replaces_todays_manifest(workspace, monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "adw-warehouse")
    monkeypatch.setenv("R2_OVERRIDE_DATE", "2026-10-16")
    fake = _patch_client(monkeypatch, FakeS3())
    assert u.main() == 0
    assert fake.uploaded == ["raw/alpine-data/2026-10-16/data-cpi.json"]
    assert fake.puts == ["raw/alpine-data/2026-10-16/MANIFEST.json"]
    body = json.loads(fake.bodies["raw/alpine-data/2026-10-16/MANIFEST.json"])
    assert body["date"] == "2026-10-16"
    assert body["files"][0]["path"] == "data-cpi.json"
    assert body["uploaded"] == 1


def test_pages_step_does_not_swallow_the_uploader_exit_code():
    import yaml
    steps = yaml.safe_load(
        (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    )["jobs"]["build"]["steps"]
    step = next(s for s in steps if s.get("name") == "Upload daily snapshot to ADW R2 archive")
    assert step.get("continue-on-error") is True
    assert "python upload_to_r2.py" in step["run"]
    assert "||" not in step["run"]
    assert "exit 0" not in step["run"]
    sidecar = next(i for i, s in enumerate(steps) if "fetch_cpi.py" in (s.get("run") or ""))
    upload = steps.index(step)
    stage = next(i for i, s in enumerate(steps) if s.get("name") == "Stage site directory")
    assert sidecar < upload < stage
