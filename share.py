"""
CLI for minting / listing / revoking share tokens.

Examples:
    # Mint a 3-day read-only share link (and print the URL):
    .venv/bin/python share.py --days 3 --label "for J. via SMS"

    # List active shares:
    .venv/bin/python share.py --list

    # Revoke a share by token (or by URL — anything containing the token):
    .venv/bin/python share.py --revoke <token-or-url>

    # Drop everything that has already expired:
    .venv/bin/python share.py --prune

The CLI does not know about your public Cloudflare Tunnel hostname, so by
default it prints links rooted at http://127.0.0.1:8765. Pass --host to print
links rooted at e.g. https://dashboard.example.com.
"""

from __future__ import annotations

import argparse
import os
import sys

import shares


def _format_share(entry: dict, host: str) -> str:
    url = host.rstrip("/") + "/share/" + entry["token"]
    label = entry.get("label") or ""
    parts = [url]
    parts.append(f"expires={entry.get('expires_at','?')}")
    if label:
        parts.append(f"label={label!r}")
    if entry.get("expired"):
        parts.append("EXPIRED")
    return "  ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Mint / list / revoke share tokens.")
    ap.add_argument("--days", type=float, default=3.0, help="Lifetime when minting (default: 3)")
    ap.add_argument("--label", default="", help="Optional human-readable label")
    ap.add_argument("--host", default=os.environ.get("SHARE_HOST", "http://127.0.0.1:8765"),
                    help="Public host root (default: $SHARE_HOST or http://127.0.0.1:8765)")
    ap.add_argument("--list", action="store_true", help="List active shares")
    ap.add_argument("--all", action="store_true", help="With --list, include expired ones")
    ap.add_argument("--revoke", metavar="TOKEN_OR_URL", help="Revoke a share")
    ap.add_argument("--prune", action="store_true", help="Drop expired share tokens")
    args = ap.parse_args()

    if args.prune:
        n = shares.prune_expired()
        print(f"Pruned {n} expired share(s).")
        return 0

    if args.revoke:
        token = args.revoke.rstrip("/")
        if "/share/" in token:
            token = token.split("/share/", 1)[1].split("/", 1)[0]
        if not token:
            print("revoke: empty token", file=sys.stderr)
            return 2
        removed = shares.revoke(token)
        print(("Revoked." if removed else "Not found.") + " token=" + token)
        return 0 if removed else 1

    if args.list:
        rows = shares.list_all(include_expired=args.all)
        if not rows:
            print("(no active shares)")
            return 0
        for r in rows:
            print(_format_share(r, args.host))
        return 0

    # Default action: mint a new share.
    try:
        entry = shares.create(days=args.days, label=args.label,
                              created_by=os.environ.get("DASH_USER", ""))
    except Exception as e:
        print(f"mint failed: {e}", file=sys.stderr)
        return 1
    url = args.host.rstrip("/") + "/share/" + entry["token"]
    print(url)
    print(f"  expires: {entry['expires_at']}", file=sys.stderr)
    if entry.get("label"):
        print(f"  label:   {entry['label']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
