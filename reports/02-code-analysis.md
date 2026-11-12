# 02 - Code analysis: why it loops

The bug is not the WebSocket/Redis architecture (socket.io with a Redis manager across pods is standard). It is a
**positive feedback loop with no damping**: load causes failures, failures cause disconnects, disconnects cause
reconnects, reconnects cause load. Each piece is reasonable on its own; together they form a retry storm.

All line numbers below were checked on the three versions. They are the same code; only the numbers differ.

| # | Component | v0.12.5 | v0.13.3 (`b0e6098`) | dev (`2547992`, 2026-11-12) |
|---|---|---|---|---|
| A | Client: on `io server disconnect` call `_socket.connect()` **immediately**, no delay | `src/routes/+layout.svelte` L286 | L293 | L295 |
| B | Client: every socket `connect` runs `refreshChatRows` -> chat list, pinned, folders, folder items | `src/lib/components/layout/Sidebar.svelte` L842 (def L533) | L864 (def L539) | L892 (def L566) |
| C | Server: socket `connect` handler verifies the token via `get_verified_user_by_token` (DB) | `backend/open_webui/socket/main.py` L611 | L620 | L625 |
| D | Server: every 30 s re-verify **all** local authenticated sockets against the DB | `socket/main.py` L461 `periodic_socket_authentication` | L463 | L468 |
| E | Server: `get_socket_session_user`: `except Exception:` -> `sio.disconnect(sid)` (any error = auth failure) | `socket/main.py` L469-476 | L478-485 | L483-490 |
| F | Auth: `is_valid_token` calls `get_auth()` (DB) on every check; docstring: "Database failures fail closed" | `backend/open_webui/utils/auth.py` L279 | L276 | L276 |

## The cycle

1. **A browser connects; the server needs the DB to accept it** (C -> F). No free pool connection within
   `DATABASE_POOL_TIMEOUT` = the token is treated as invalid.
2. **The connect handler raises `sqlalchemy.exc.TimeoutError` and the WebSocket is torn down.** *(observed in the
   reproduction logs, not traced inside python-socketio)*:
   ```
   File "/app/backend/open_webui/socket/main.py", line 626, in connect
       user = await get_verified_user_by_token(auth['token'], redis)
   ...
       if decoded is None or 'id' not in decoded or not await is_valid_token(decoded, redis):
   sqlalchemy.exc.TimeoutError: QueuePool limit of size 2 overflow 0 reached, connection timed out, timeout 5.00
   ```
3. **The browser reconnects quickly.** After a torn-down transport socket.io retries with its built-in 1-5 s backoff
   (`+layout.svelte` L188-191). After a server-initiated disconnect (step 5) it reconnects with **no delay at all** (A).
4. **Every successful reconnect generates more DB work** (B): chat list + pinned + folders + per-folder items. These
   requests occupy the pool. In the reproduction data `chats` is exactly 2 x `ws_new` in every window.
5. **Every 30 s the server re-verifies all existing sockets against the same saturated DB** (D -> F) and
   disconnects every socket whose check fails for any reason, including a pool timeout (E). Those browsers go to
   step 3 on the no-delay path.

## Why it never recovers on its own

The pool is occupied by requests that the reconnects themselves generate (step 4). The next connect's token check
(step 1) waits behind them, times out, fails (step 2), reconnects (step 3) and generates more of step 4. The load
that saturates the pool is a product of the loop, so the original trigger going away changes nothing. In the
reproduction PostgreSQL was back after 45 s and six minutes later there were still 10-29 pool timeouts per 15 s with
no downward trend.

## The second loop: invalid tokens (after a mass revoke)

A tab holding a revoked token gets 401 on its requests. The client then calls `GET /api/v1/auths/` to confirm the
session is dead before signing out; if that request **fails or times out** (which it does when the app is overloaded)
the tab does not sign out and keeps reconnecting and requesting. Each 401 still costs a JWT decode and a DB
`get_auth` lookup, so thousands per second show up as app CPU and, eventually, DB load again. This is the Nov 8
pattern (3 tabs) and the Nov 12-afternoon pattern (many tabs after the revoke).

## What is verified vs. inferred

- Verified by reading code: A, B, C, D, E, F.
- Observed in reproduction logs only: step 2 (the transport is torn down when the connect handler raises).
- Inferred: in production, Redis loss produced the first DB stall that started the loop; the reproduction only
  shows that a DB stall is sufficient.

## Suggested fixes (for the upstream issue)

1. Distinguish "token invalid" from "could not verify" in C/D/E: on a transient backend error keep the socket and
   retry later instead of disconnecting.
2. Jittered exponential backoff before `_socket.connect()` after `io server disconnect` (A).
3. Do not refresh the whole sidebar on every reconnect; refresh only when something changed, or debounce (B).
4. Spread the periodic re-verification over time and/or cache token validity briefly (D/F) so the DB is not a
   single point of failure for the realtime layer.
5. Client: when the sign-out confirmation request fails, retry with backoff instead of staying in the loop.
