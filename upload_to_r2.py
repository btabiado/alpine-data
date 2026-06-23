"""
upload_to_r2.py — Daily snapshot archive to ADW R2 warehouse.

Globs all data-*.json files in the repo root and v2/ subdirectory,
uploads them to s3://adw-warehouse/raw/alpine-data/{YYYY-MM-DD}/
(preserving the v2/ subfolder in the key), then uploads a MANIFEST.json
listing each file's path, size, and sha256.

Run by the pages.yml CI step immediately after data-*.json files are
final (post-build, pre-deploy). Never called locally — requires R2 creds.

Exit 0 always (partial failures are logged; CI uses continue-on-error).
"""

import os
import sys
import json
import hashlib
import glob
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ACCOUNT_ID = "d486b561a8eacd568dd8edf9c749ee47"
R2_ENDPOINT_URL = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"


def sha256_of(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    bucket_name = os.environ.get("R2_BUCKET_NAME")
    if not bucket_name:
        print("[r2] ERROR: R2_BUCKET_NAME env var not set — skipping upload.")
        sys.exit(0)  # exit 0 so CI never blocks on missing env

    # boto3 picks up AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY automatically
    s3 = boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT_URL,
        region_name="auto",
    )

    # Backfill support: r2-backfill.yml workflow sets R2_OVERRIDE_DATE per historical
    # commit so the same script can be reused to walk git history and stash each
    # day's snapshot under its true date instead of today's.
    override_date = os.environ.get("R2_OVERRIDE_DATE", "").strip()
    if override_date:
        # Basic shape check — workflow guarantees YYYY-MM-DD
        if len(override_date) != 10 or override_date[4] != "-" or override_date[7] != "-":
            print(f"[r2] ERROR: invalid R2_OVERRIDE_DATE '{override_date}' — must be YYYY-MM-DD.")
            sys.exit(0)
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
        print("[r2] No data-*.json files found — nothing to upload.")
        return

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
            print(f"[r2] WARN: delta-mode listing failed ({exc}) — falling back to full upload")
            existing_keys = set()  # safest fallback: upload everything

    total = len(files_to_upload)
    uploaded = 0
    skipped = 0
    total_bytes = 0
    manifest_entries = []

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
            print(f"[r2] WARN: failed to upload {r2_key}: {exc}")

    # Upload MANIFEST.json (skip if delta mode AND nothing was uploaded this run —
    # the existing per-day MANIFEST stays authoritative).
    if uploaded > 0 or not skip_if_present:
        manifest_key = f"{date_prefix}/MANIFEST.json"
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
            print(f"[r2] WARN: failed to upload manifest: {exc}")

    total_kb = total_bytes / 1024.0
    mode_str = "delta" if skip_if_present else "refresh"
    print(f"[r2] {mode_str} mode: uploaded {uploaded}/{total} files, skipped {skipped} ({total_kb:.0f} KB written)")


if __name__ == "__main__":
    main()
