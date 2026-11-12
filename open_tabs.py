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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        pages = []
        for u in tokens:
            ctx = browser.new_context(viewport={"width": 1200, "height": 800})
            # Same mechanism the real UI uses: token in localStorage, read on page load.
            ctx.add_init_script(
                f"if (!localStorage.getItem('token')) localStorage.setItem('token', {json.dumps(u['token'])});"
            )
            for _ in range(args.tabs_per_user):
                page = ctx.new_page()
                page.on("request", on_request)
                page.on("response", on_response)
                page.on("websocket", on_websocket)
                page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
                pages.append(page)
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

        browser.close()
    print("Tabs closed.")


if __name__ == "__main__":
    main()
