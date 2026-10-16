"""
upload_to_r2.py — Daily snapshot archive to ADW R2 warehouse.

Globs all data-*.json files in the repo root and v2/ subdirectory,
uploads them to s3://adw-warehouse/raw/alpine-data/{YYYY-MM-DD}/
(preserving the v2/ subfolder in the key), then uploads a MANIFEST.json
listing each file's path, size, and sha256.

Run by the pages.yml CI step as soon as data-*.json files are final, and by
r2-backfill.yml with R2_OVERRIDE_DATE set. Requires R2 creds.

Exit 0 only when every selected object was uploaded or (delta mode) was
already present. Missing creds, a bad override date, an empty file list, a
failed delta listing, or any failed PUT exits 1 and prints a ::error::
annotation. pages.yml keeps the step continue-on-error so an R2 outage still
cannot block the deploy, but the step itself goes red. Delta mode
(R2_SKIP_IF_PRESENT) never overwrites an existing object, including
MANIFEST.json; a listing failure refuses to upload rather than falling
through to a full overwrite.
"""

import os
import sys
import json
import hashlib
import glob
from datetime import datetime, timezone

# boto3 is installed by pages.yml / r2-backfill.yml (requirements-jobs.txt).
# The unit-test job installs requirements.txt only, so this import must not
# fail collection when boto3 is absent. The stubs let the except clauses below
# stay the same; main() reports the missing library instead of crashing.
try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except ImportError:
    boto3 = None

    class BotoCoreError(Exception):
        pass

    class ClientError(BotoCoreError):
        pass

ACCOUNT_ID = "d486b561a8eacd568dd8edf9c749ee47"
R2_ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"


def sha256_of(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _fail(message):
    """Print a log line and a ::error:: annotation. Returns the process code.

    pages.yml and r2-backfill.yml both surface this. The step summary is
    written when Actions provides one, so the failure is visible without
    opening the raw log.
    """
    print(f"[r2] ERROR: {message}")
    print(f"::error title=R2 archive failed::{message}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(
                "\n## R2 daily archive failed\n\n"
                f"{message}\n\n"
                "The site deploy is non-blocking on purpose: an R2 outage must "
                "not withhold the dashboard. This step is red because the "
                "archive did not complete. Re-run pages, or run r2-backfill in "
                "delta mode for the missing UTC date (delta does not overwrite "
                "objects that are already there).\n"
            )
    return 1


def main():
    bucket_name = os.environ.get("R2_BUCKET_NAME")
    if not bucket_name:
        return _fail("R2_BUCKET_NAME is unset; no daily payload was archived.")
    if boto3 is None:
        return _fail("boto3 is not installed; no daily payload was archived.")

    # boto3 picks up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY automatically
    try:
        s3 = boto3.client(
            "s3",
            endpoint_url=R2_ENDPOINT_URL,
            region_name="auto",
        )
    except (BotoCoreError, ClientError) as exc:
        return _fail(f"could not create the R2 client: {exc}")

    # Backfill support: r2-backfill.yml workflow sets R2_OVERRIDE_DATE per historical
    # commit so the same script can be reused to walk git history and stash each
    # day's snapshot under its true date instead of today's.
    override_date = os.environ.get("R2_OVERRIDE_DATE", "").strip()
    if override_date:
        # Basic shape check — workflow guarantees YYYY-MM-DD
        if len(override_date) != 10 or override_date[4] != "-" or override_date[7] != "-":
            return _fail(
                f"invalid R2_OVERRIDE_DATE '{override_date}' — must be YYYY-MM-DD."
            )
        today = override_date
        print(f"[r2] backfill mode: using override date {today}")
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_prefix = f"raw/alpine-data/{today}"

    # Collect files — preserve relative path for key construction.
    # Globs are intentionally relative so the key mirrors the repo layout:
    #   data-foo.json        → raw/alpine-data/{date}/data-foo.json
    #   v2/data-bar.json     → raw/alpine-data/{date}/v2/data-bar.json
    patterns = ["data-*.json", "v2/data-*.json"]
    candidates = []
    for pattern in patterns:
        candidates.extend(glob.glob(pattern))
    files_to_upload = sorted(set(candidates))

    if not files_to_upload:
        return _fail(
            "No data-*.json files found; nothing was archived. "
            "pages.yml should always have the committed sidecars on disk."
        )

    # Delta mode: skip files already present in R2 for this date.
    # r2-backfill.yml sets R2_SKIP_IF_PRESENT=true when mode=delta (default).
    # mode=refresh leaves it unset → blast-upload as before.
    # pages.yml NEVER sets it — today's data is re-generated every cron run
    # and we want each new snapshot to overwrite.
    skip_if_present = os.environ.get("R2_SKIP_IF_PRESENT", "").lower() in ("true", "1", "yes")
    existing_keys = set()
    if skip_if_present:
        try:
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket_name, Prefix=f"{date_prefix}/"):
                for obj in page.get("Contents", []):
                    existing_keys.add(obj["Key"])
            print(f"[r2] delta mode: {len(existing_keys)} keys already present under {date_prefix}/ — will skip those")
        except (BotoCoreError, ClientError) as exc:
            # Falling through to a full PUT would overwrite every object under
            # this date. Delta mode is the backfill's "do not clobber" path,
            # so a listing failure is a hard stop.
            return _fail(
                f"delta-mode listing failed ({exc}); refusing to upload so "
                f"existing objects under {date_prefix}/ are left untouched."
            )

    total = len(files_to_upload)
    uploaded = 0
    skipped = 0
    total_bytes = 0
    manifest_entries = []
    failed_files = []

    for rel_path in files_to_upload:
        r2_key = f"{date_prefix}/{rel_path}"
        if skip_if_present and r2_key in existing_keys:
            skipped += 1
            continue
        try:
            size = os.path.getsize(rel_path)
            digest = sha256_of(rel_path)
            with open(rel_path, "rb") as fh:
                s3.upload_fileobj(fh, bucket_name, r2_key)
            kb = size / 1024.0
            print(f"[r2] uploaded {r2_key} ({kb:.0f} KB)")
            manifest_entries.append({
                "path": rel_path,
                "r2_key": r2_key,
                "size": size,
                "sha256": digest,
            })
            uploaded += 1
            total_bytes += size
        except (BotoCoreError, ClientError, OSError) as exc:
            failed_files.append(rel_path)
            print(f"[r2] ERROR: failed to upload {r2_key}: {exc}")

    # MANIFEST.json. Daily pages runs (no skip flag) replace today's manifest
    # with this run's file list. Delta mode must not overwrite an existing
    # manifest: a partial rewrite would drop every file this run skipped.
    manifest_key = f"{date_prefix}/MANIFEST.json"
    write_manifest = uploaded > 0 or not skip_if_present
    if skip_if_present and manifest_key in existing_keys:
        print(f"[r2] delta mode: {manifest_key} already present — leaving it untouched")
        write_manifest = False
    if write_manifest:
        manifest_payload = {
            "date": today,
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "mode": "delta" if skip_if_present else "refresh",
            "files": manifest_entries,
            "uploaded": uploaded,
            "skipped": skipped,
            "total": total,
        }
        try:
            s3.put_object(
                Bucket=bucket_name,
                Key=manifest_key,
                Body=json.dumps(manifest_payload, indent=2).encode(),
                ContentType="application/json",
            )
            print(f"[r2] uploaded {manifest_key}")
        except (BotoCoreError, ClientError) as exc:
            failed_files.append("MANIFEST.json")
            print(f"[r2] ERROR: failed to upload manifest: {exc}")

    total_kb = total_bytes / 1024.0
    mode_str = "delta" if skip_if_present else "refresh"
    # Backfill parses this exact shape. Print it even when the run failed so
    # a partial day still counts the objects that did land.
    print(f"[r2] {mode_str} mode: uploaded {uploaded}/{total} files, skipped {skipped} ({total_kb:.0f} KB written)")
    if failed_files:
        shown = ", ".join(failed_files[:8])
        extra = f" (+{len(failed_files) - 8} more)" if len(failed_files) > 8 else ""
        return _fail(
            f"{mode_str} mode archived {uploaded}/{total} files for {today}; "
            f"{len(failed_files)} failed: {shown}{extra}."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
