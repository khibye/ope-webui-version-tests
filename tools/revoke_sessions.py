#!/usr/bin/env python3
"""
Revoke ("Sign out all devices") Open WebUI sessions for users active in the last N days.

Uses only the Open WebUI admin API:
  GET  /api/v1/auths/                          -> the admin running the script (skipped)
  GET  /api/v1/users/all                       -> all users with last_active_at
  POST /api/v1/users/{id}/sessions/revoke      -> same as "Sign out all devices" in Admin Panel

Python 3.8+, standard library only.

Usage:
  export OWUI_URL="https://chat.example.com"
  export OWUI_TOKEN="<admin token>"   # browser console as admin: localStorage.token
  python3 revoke_sessions.py --days 7 --dry-run      # list who would be signed out
  python3 revoke_sessions.py --days 7 --workers 10   # actually revoke
"""

import argparse
import json
import os
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed


def build_ctx(insecure):
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def request(method, url, token, ctx, timeout=60):
    req = urllib.request.Request(
        url,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        data=b"" if method == "POST" else None,
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        body = resp.read()
        return resp.status, (json.loads(body) if body else None)


def revoke_one(base, token, ctx, user_id, retries=3):
    """Return (user_id, status_code_or_error). Retries on 5xx / network errors."""
    url = f"{base}/api/v1/users/{user_id}/sessions/revoke"
    delay = 1.0
    for attempt in range(1, retries + 1):
        try:
            status, _ = request("POST", url, token, ctx, timeout=30)
            return user_id, status
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == retries:
                return user_id, e.code  # 4xx is final (401 = admin token died, 403 = first admin, 404 = gone)
        except Exception as e:  # timeout / connection reset
            if attempt == retries:
                return user_id, f"error: {e}"
        time.sleep(delay)
        delay *= 2


def main():
    p = argparse.ArgumentParser(description="Sign out Open WebUI users active in the last N days.")
    p.add_argument("--days", type=float, default=7, help="only users active in the last N days (default 7)")
    p.add_argument("--workers", type=int, default=10, help="parallel requests (default 10)")
    p.add_argument("--dry-run", action="store_true", help="only print how many users would be signed out")
    p.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed certs)")
    args = p.parse_args()

    base = os.environ.get("OWUI_URL", "").rstrip("/")
    token = os.environ.get("OWUI_TOKEN", "")
    if not base or not token:
        sys.exit("Set OWUI_URL and OWUI_TOKEN environment variables.")
    ctx = build_ctx(args.insecure)

    # 1. Who am I (so we don't revoke our own token mid-run)
    try:
        _, me = request("GET", f"{base}/api/v1/auths/", token, ctx)
    except urllib.error.HTTPError as e:
        sys.exit(f"Admin token check failed: HTTP {e.code}. Is OWUI_TOKEN valid?")
    my_id = me["id"]
    print(f"Running as {me.get('email')} ({my_id}) - this user is skipped.")

    # 2. All users
    print("Fetching users ...")
    _, data = request("GET", f"{base}/api/v1/users/all", token, ctx, timeout=300)
    users = data.get("users", [])
    cutoff = time.time() - args.days * 86400
    targets = [
        u["id"]
        for u in users
        if u["id"] != my_id and (u.get("last_active_at") or 0) >= cutoff
    ]
    print(f"Total users: {len(users)} | active in last {args.days:g} days (excluding you): {len(targets)}")

    if args.dry_run:
        print("Dry run - nothing revoked.")
        return
    if not targets:
        return

    # 3. Revoke in parallel
    ok, failed = 0, []
    lock = threading.Lock()
    stop = threading.Event()
    start = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(revoke_one, base, token, ctx, uid) for uid in targets]
        for i, fut in enumerate(as_completed(futures), 1):
            uid, status = fut.result()
            with lock:
                if status == 200:
                    ok += 1
                else:
                    failed.append((uid, status))
                    if status == 401 and not stop.is_set():
                        stop.set()
                        print("\nHTTP 401: the admin token is no longer valid. Stopping.", file=sys.stderr)
                        for f in futures:
                            f.cancel()
            if i % 200 == 0 or i == len(targets):
                rate = i / max(time.time() - start, 0.001)
                print(f"  {i}/{len(targets)}  ok={ok}  failed={len(failed)}  ({rate:.0f}/s)")

    print(f"\nDone in {time.time() - start:.0f}s: {ok} signed out, {len(failed)} failed.")
    if failed:
        with open("revoke_failed.txt", "w") as f:
            for uid, status in failed:
                f.write(f"{uid}\t{status}\n")
        print("Failed IDs written to revoke_failed.txt (403 on the first admin account is expected).")


if __name__ == "__main__":
    main()
