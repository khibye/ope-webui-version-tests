# 06 - Fix verification

Branch: `fix/socket-reconnect-storm`. Fix: `patches/apply_fix.py` (diff: `patches/socket-reconnect-storm-v0.13.3.diff`).
Workflow: `.github/workflows/verify-fix.yml` - clones Open WebUI at the ref, applies the fix, rebuilds the frontend,
layers the build and the patched `socket/main.py` onto the official image, then runs the same reproduction as 03.

Run: https://github.com/khibye/ope-webui-version-tests/actions/runs/43628125107 (2026-11-14)

## Results

Same setup as 03: 20 logged-in tabs, pool size 2 / overflow 0 / timeout 5 s, +100 ms DB latency from the trigger on.

| image | upstream revision | trigger | pool timeouts | socket reconnects after trigger | windows >2 min after trigger with timeouts | verdict |
|---|---|---|---|---|---|---|
| **unpatched** v0.13.3 | b0e6098 | PostgreSQL paused 45 s | 347 | 214 | 16 / 16 | loop (03) |
| **unpatched** dev | 2547992 | PostgreSQL paused 45 s | 602 | 205 | 17 / 17 | loop (03) |
| **patched** v0.13.3 | b0e6098 | PostgreSQL paused 45 s | **0** | **0** | **0 / 17** | no loop |
| **patched** dev | fcff231 | PostgreSQL paused 45 s | **0** | **0** | **0 / 17** | no loop |
| **patched** v0.13.3 | b0e6098 | Redis removed 60 s | 0 | 0 | 0 / 17 | no loop (same as unpatched) |
| **patched** dev | fcff231 | Redis removed 60 s | 0 | 0 | 0 / 17 | no loop (same as unpatched) |

## v0.12.5 (production version), branch `fix/socket-reconnect-storm-v0.12.5`

Run: https://github.com/khibye/ope-webui-version-tests/actions/runs/43699314906 (2026-11-15)

| image | upstream revision | trigger | pool timeouts | socket reconnects after trigger | windows >2 min after trigger with timeouts | verdict |
|---|---|---|---|---|---|---|
| **patched** v0.12.5 | f7ac7ee | PostgreSQL paused 45 s | **0** | **0** | **0 / 17** | no loop (30 x "keeping the session") |
| **patched** v0.12.5 | f7ac7ee | Redis removed 60 s | 0 | 0 | 0 / 17 | no loop; 5 pre-existing heartbeat/Redis DNS tracebacks, same as unpatched |

Published image: `ghcr.io/khibye/ope-webui-version-tests/open-webui:v0.12.5-fix`
(immutable: `...:v0.12.5-fix-a2ab2922453759c37f82b2d1e274d1205d9817b1`) = official `v0.12.5` + rebuilt frontend +
patched `backend/open_webui/socket/main.py`. No schema or migration changes; rollback = switch the tag back.

## Fix #2: the invalid-token (401) loop - v0.12.5

Branch `fix/socket-reconnect-storm-v0.12.5`, commit `88d6a0f`. Run: https://github.com/khibye/ope-webui-version-tests/actions/runs/43725755241 (2026-11-15)

Scenario `revoke`: 20 tabs (5 users x 4), the admin revokes the sessions of the other 4 users while their 16 tabs
are open - what a mass "Sign out all devices" does in production.

| image | 401 responses total / last 2 min | socket reconnects after trigger / last 2 min | tabs on login page at the end | verdict |
|---|---|---|---|---|
| **unpatched** v0.12.5 | 585 / **160** | 224 / **64** | 8 of 16 | **LOOPING** - steady 8 reconnects + 20 x 401 every 10 s, for the whole 6 min |
| **patched** v0.12.5 | 92 / **0** | 48 / **0** | 8 of 16 | **FIXED** - storm over ~80 s after the revoke, then silence |

Mechanism (unpatched): a socket whose token is invalid is accepted without a user; every 30 s heartbeat the server
disconnects it; the client reconnects immediately and re-fetches chats/pinned/folders -> 401 x3; the sign-out path
needs `GET /api/v1/auths/` to answer exactly 401, which fails under load, so the tab never leaves.

Fix #2: the server refuses the handshake of an invalid token (`unauthorized`) and never disconnects anonymous
sockets on heartbeat; the client signs out on that refusal and reconnects anonymously (login page keeps a socket).
Works for tabs still running the old frontend too: a refused handshake is not retried by socket.io.

The 8 tabs that did not reach the login page in either run are sibling tabs of the same user: one tab removes the
shared `localStorage.token`, the others keep a stale UI with an anonymous socket. With the patch they are quiet
(0 requests); commit `after 88d6a0f` adds a `storage` listener so they sign out too.

Published image (fix #1 + #2): `ghcr.io/khibye/ope-webui-version-tests/open-webui:v0.12.5-fix-88d6a0f70614f855a5bba06e26a86cc2b9ea8e27`

## Fix #3: cheaper keepalive, bounded sweep - v0.12.5

Commit `aebacef`. Run: https://github.com/khibye/ope-webui-version-tests/actions/runs/43850362548 (2026-11-16)

Motivation: with the storms gone, production pods (one uvicorn worker each) stayed CPU-bound while PostgreSQL was
nearly idle (1 active query, 40 connections "idle in transaction"). In 0.12.5 every connected socket costs, while
doing nothing: a heartbeat every 30 s = JWT decode + 2 DB reads + `UPDATE user.last_active_at` + commit + 2 blocking
Redis calls, plus one more DB read per socket per sweep; and each sweep check waited up to `DATABASE_POOL_TIMEOUT`
(30 s in production) for a pool connection, so one sweep over a few hundred sockets could run for hours.

Changes: heartbeat only refreshes `last_seen` in Redis and writes `last_active_at` at most every
`HEARTBEAT_LAST_ACTIVE_WRITE_INTERVAL` (300 s); each sweep check is bounded by `SOCKET_REAUTH_CHECK_TIMEOUT` (5 s,
overrun = "could not verify", session kept); the sweep yields every 50 sockets and warns when it overruns
`SOCKET_REAUTH_INTERVAL` (env, default 30). Token revocation is still enforced by the sweep every 30-45 s.

| trigger | pool timeouts | reconnects after trigger | 401 last 2 min | tabs on login page | verdict |
|---|---|---|---|---|---|
| PostgreSQL paused 45 s | 0 | 0 | - | - | no loop |
| Redis removed 60 s | 0 | 0 | - | - | no loop (6 pre-existing tracebacks in Redis listeners, as unpatched) |
| revoke all sessions | 0 | 47 (burst, then 0) | 0 | 16 / 16 | FIXED |

During the DB pause the sweep logged `Socket re-verification sweep took 69s for 20 sockets (interval 30s)`: expected,
each check hit its 5 s cap. Next improvement if that warning shows up in production: a per-sweep deadline that stops at
the interval and resumes from the same position next time.

Published image (fix #1 + #2 + #3): `ghcr.io/khibye/ope-webui-version-tests/open-webui:v0.12.5-fix-aebacef670e46c41c9c03dc19284431efaaf602b`

## Proof that the stall happened and the new code path handled it

During the 45 s pause the periodic re-verification sweep did hit the dead database. With the fix the server logged,
32 times (v0.13.3) / 34 times (dev):

```
Could not re-verify socket <sid> because the backend is unavailable (503: Authentication is temporarily unavailable.); keeping the session
```

and disconnected nothing. No disconnect -> no reconnect -> no sidebar refresh burst -> the pool never saturated:
0 `QueuePool` errors, 0 new WebSocket connections on the browser side, 0 tracebacks, for the whole 6-minute
observation. Unpatched, the same trigger produced the first disconnects within 15 s and a loop that was still running
six minutes later.

## What the fix changes (see 02 for the loop)

Server (`backend/open_webui/socket/main.py`):
- `is_transient_backend_error()`: SQLAlchemy / Redis / timeout / connection errors and `get_auth()`'s 503 are
  "could not verify", not "invalid token".
- `get_socket_session_user()`: on a transient error keep the previously verified session user and retry on the next
  sweep; disconnect only on a real invalid token.
- `connect()`: on a transient error refuse the handshake with `ConnectionRefusedError('backend_unavailable', {retry: true})`
  instead of letting the exception tear down the transport.
- `user-join`: returns `{error: 'backend_unavailable', retry: true}` instead of failing silently.
- `periodic_socket_authentication()`: 0-15 s jitter on the 30 s sweep.

Client:
- `src/routes/+layout.svelte`: reconnect after `io server disconnect` or a refused handshake with exponential backoff
  and jitter (1 s .. 30 s, reset on successful connect) instead of immediately; retry `user-join` with backoff on
  `backend_unavailable`.
- `src/lib/components/layout/Sidebar.svelte`: skip reconnect-triggered full sidebar refreshes that follow each other
  within 15 s.

## Caveats

- The `dev` test pairs a frontend built from dev HEAD (`fcff231`) with the backend of the official `:dev` image
  (plus the patched `socket/main.py`); the two may be a few commits apart. The v0.13.3 pair is exact.
- Keeping a session for up to one sweep interval when the backend is unreachable means a token revoked during an
  outage stays usable on an already-open socket until the backend is back (at most ~45 s longer than today). HTTP
  requests are unaffected: they still fail closed.
- Not tested: multi-worker / multi-pod deployments, `redis-oom` mode against the patched image.
