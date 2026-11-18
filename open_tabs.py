#!/usr/bin/env python3
"""
Open many logged-in Open WebUI tabs with Playwright and count what they do.

Every 10 s it prints, for the last window:
  ws_new      new WebSocket connections opened (= socket.io reconnects)
  version     GET /api/version
  chats       GET /api/v1/chats/...  (list, pinned, ...)
  folders     GET /api/v1/folders/
  http_401 / http_5xx  error responses
Also appends the same numbers to tabs_stats.csv.

A healthy, idle set of tabs shows ~0 in every column. A reconnect storm shows
ws_new climbing every window together with version/chats/folders.

Install once:  pip install playwright && python3 -m playwright install chromium
Usage:         python3 open_tabs.py --url http://localhost:3000 --users 5 --tabs-per-user 4 --duration 900
"""
import argparse
import csv
import json
import re
import signal
import sys
import threading
import time
from collections import Counter

from playwright.sync_api import sync_playwright

PATTERNS = {
    "version": re.compile(r"/api/version(\?|$)"),
    "chats": re.compile(r"/api/v1/chats/"),
    "folders": re.compile(r"/api/v1/folders/"),
    "auths": re.compile(r"/api/v1/auths/(\?|$)"),
}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def norm_path(url):
    path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
    return "/" + UUID.sub("{id}", path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:3000")
    ap.add_argument("--tokens", default="tokens.json")
    ap.add_argument("--users", type=int, default=5)
    ap.add_argument("--tabs-per-user", type=int, default=4)
    ap.add_argument("--duration", type=int, default=900, help="seconds to keep tabs open")
    ap.add_argument("--interval", type=int, default=10)
    ap.add_argument("--csv", default="tabs_stats.csv")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--open-chat", action="store_true", help="create one chat per user and open the tabs on it")
    ap.add_argument("--request-log", default="", help="write every response (time, tab, user, method, path, status) here")
    ap.add_argument("--final-state", default="", help="write each tab's final state (JSON) here")
    args = ap.parse_args()

    with open(args.tokens) as f:
        tokens = json.load(f)[: args.users]
    if not tokens:
        sys.exit("tokens.json is empty - run setup_users.py first")

    counts = Counter()
    lock = threading.Lock()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def bump(key):
        with lock:
            counts[key] += 1

    def on_request(req):
        url = req.url
        for key, pat in PATTERNS.items():
            if pat.search(url):
                bump(key)
                break

    def on_response(resp):
        s = resp.status
        if s == 401:
            bump("http_401")
        elif s >= 500:
            bump("http_5xx")
        elif s == 429:
            bump("http_429")

    def on_websocket(ws):
        bump("ws_new")

    req_log = open(args.request_log, "w") if args.request_log else None
    if req_log:
        req_log.write("ts,tab,user,method,path,status\n")

    def log_response(resp, tab, user):
        if req_log is None:
            return
        path = norm_path(resp.url)
        if path.startswith("/_app/") or path.startswith("/static/") or path.endswith((".js", ".css", ".png", ".svg", ".woff2")):
            return
        with lock:
            req_log.write(f"{time.time():.3f},{tab},{user},{resp.request.method},{path},{resp.status}\n")

    def log_failed(req, tab, user):
        if req_log is None:
            return
        path = norm_path(req.url)
        if path.startswith("/_app/") or path.startswith("/static/"):
            return
        with lock:
            req_log.write(f"{time.time():.3f},{tab},{user},{req.method},{path},ERR\n")

    def create_chat(token):
        import urllib.request as _u
        body = json.dumps({"chat": {"title": "repro chat", "models": [], "messages": [],
                                    "history": {"messages": {}, "currentId": None}}}).encode()
        r = _u.Request(args.url.rstrip("/") + "/api/v1/chats/new", data=body, method="POST",
                       headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with _u.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())["id"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        pages = []
        tab_meta = []
        for ui, u in enumerate(tokens):
            ctx = browser.new_context(viewport={"width": 1200, "height": 800})
            # Seed the token once per tab, as a sign-in would. sessionStorage survives a reload of the same tab,
            # so a tab that later signs out or reloads is NOT given the token again (that would skew the test).
            ctx.add_init_script(
                "if (!sessionStorage.getItem('repro_seeded')) {"
                f" localStorage.setItem('token', {json.dumps(u['token'])}); sessionStorage.setItem('repro_seeded', '1'); }}"
            )
            # A UI sign-in also sets the token cookie; the backend accepts it when no Authorization header is sent.
            ctx.add_cookies([{"name": "token", "value": u["token"], "url": args.url}])
            target = args.url
            if args.open_chat:
                target = args.url.rstrip("/") + "/c/" + create_chat(u["token"])
            for _ in range(args.tabs_per_user):
                page = ctx.new_page()
                tab = len(pages)
                page.on("request", on_request)
                page.on("response", on_response)
                page.on("websocket", on_websocket)
                page.on("response", lambda r, t=tab, x=ui: log_response(r, t, x))
                page.on("requestfailed", lambda r, t=tab, x=ui: log_failed(r, t, x))
                page.goto(target, wait_until="domcontentloaded", timeout=60000)
                pages.append(page)
                tab_meta.append({"tab": tab, "user": ui})
        print(f"Opened {len(pages)} tabs for {len(tokens)} users. Warming up 20 s ...", flush=True)
        # NOTE: with the sync API, page events are only dispatched while a Playwright call is
        # running, so we must wait through Playwright, never time.sleep().
        pages[0].wait_for_timeout(20000)
        # Diagnostics: are the tabs really logged in and connected?
        logged_in = on_auth = has_token = 0
        for pg in pages:
            try:
                info = pg.evaluate(
                    "() => ({url: location.pathname, token: !!localStorage.getItem('token'),"
                    " login: !!document.querySelector('input[type=password]')})"
                )
            except Exception:
                info = {"url": "?", "token": False, "login": False}
            has_token += bool(info["token"])
            on_auth += info["url"].startswith("/auth") or info["login"]
            logged_in += (not info["url"].startswith("/auth")) and not info["login"]
        with lock:
            print(
                f"DIAG tabs={len(pages)} logged_in={logged_in} on_login_page={on_auth} "
                f"has_token={has_token} ws_opened_warmup={counts.get('ws_new', 0)} "
                f"http_401_warmup={counts.get('http_401', 0)} chats_warmup={counts.get('chats', 0)}",
                flush=True,
            )
            counts.clear()  # ignore the initial page loads

        cols = ["ts", "ws_new", "version", "chats", "folders", "auths", "http_401", "http_429", "http_5xx"]
        with open(args.csv, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(cols)
            print(" ".join(f"{c:>9s}" for c in cols), flush=True)
            start = time.time()
            while not stop.is_set() and time.time() - start < args.duration:
                pages[0].wait_for_timeout(args.interval * 1000)
                with lock:
                    row = [time.strftime("%H:%M:%S")] + [counts.get(c, 0) for c in cols[1:]]
                    counts.clear()
                w.writerow(row)
                f.flush()
                print(" ".join(f"{str(v):>9s}" for v in row), flush=True)

        # Final state of the tabs (used by the revoke mode: tabs with revoked tokens must end on the login page)
        on_auth = 0
        for pg in pages:
            try:
                info = pg.evaluate("() => ({url: location.pathname, login: !!document.querySelector('input[type=password]')})")
                on_auth += info["url"].startswith("/auth") or info["login"]
            except Exception:
                pass
        print(f"DIAG_END tabs={len(pages)} on_login_page={on_auth}", flush=True)
        if args.final_state:
            states = []
            for meta, pg in zip(tab_meta, pages):
                try:
                    st = pg.evaluate("() => ({path: location.pathname, token: !!localStorage.getItem('token'),"
                                     " login: !!document.querySelector('input[type=password]')})")
                except Exception as e:
                    st = {"error": str(e)[:80]}
                states.append({**meta, **st})
            with open(args.final_state, "w") as f:
                json.dump(states, f, indent=1)
        if req_log:
            req_log.close()
        browser.close()
    print("Tabs closed.")


if __name__ == "__main__":
    main()
