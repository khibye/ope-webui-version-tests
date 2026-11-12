#!/usr/bin/env python3
"""
Create N test users in a fresh Open WebUI instance and save their tokens.

The first signup on an empty instance becomes admin. Newer versions disable public signup
after that, so the remaining users are created by the admin via /api/v1/auths/add (role=user)
and then signed in to obtain their tokens. Existing users are simply signed in.

Usage:  python3 setup_users.py --url http://localhost:3000 --users 5
Output: tokens.json  ->  [{"email": ..., "token": ...}, ...]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

PASSWORD = "Repro-Pass-1234!"


def post(url, payload, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def wait_for_health(base, timeout=300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=5) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(3)
    sys.exit(f"{base}/health did not become ready within {timeout}s")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--out", default="tokens.json")
    args = ap.parse_args()
    base = args.url.rstrip("/")

    wait_for_health(base)
    users = []
    admin_token = None
    for i in range(args.users):
        email = f"repro{i:03d}@example.com"
        name = f"Repro User {i:03d}"
        if i == 0:
            # First user on an empty instance: public signup -> becomes admin.
            try:
                res = post(f"{base}/api/v1/auths/signup", {"name": name, "email": email, "password": PASSWORD})
                how = "signup"
            except urllib.error.HTTPError as e:
                if e.code not in (400, 403, 409):
                    raise
                res = post(f"{base}/api/v1/auths/signin", {"email": email, "password": PASSWORD})
                how = "signin"
            admin_token = res.get("token")
        else:
            # 0.13.x disables public signup once an admin exists, so the admin creates the rest.
            try:
                post(f"{base}/api/v1/auths/add",
                     {"name": name, "email": email, "password": PASSWORD, "role": "user"}, token=admin_token)
                how = "add"
            except urllib.error.HTTPError as e:
                if e.code not in (400, 409):
                    raise
                how = "exists"
            res = post(f"{base}/api/v1/auths/signin", {"email": email, "password": PASSWORD})
        token = res.get("token")
        if not token:
            sys.exit(f"No token returned for {email}: {res}")
        users.append({"email": email, "token": token, "role": res.get("role")})
        print(f"{how:6s} {email}  role={res.get('role')}")

    with open(args.out, "w") as f:
        json.dump(users, f, indent=2)
    print(f"\nSaved {len(users)} tokens to {args.out}")


if __name__ == "__main__":
    main()
