# Results

Workflow run: https://github.com/khibye/ope-webui-version-tests/actions/runs/43204023545 (2026-11-12)

Setup for every job: 1 Open WebUI container, PostgreSQL 16 behind toxiproxy, Redis 7 (`WEBSOCKET_MANAGER=redis`),
`DATABASE_POOL_SIZE=2`, `DATABASE_POOL_MAX_OVERFLOW=0`, `DATABASE_POOL_TIMEOUT=5`, 5 users x 4 tabs = 20 logged-in
Chromium tabs (verified: `DIAG tabs=20 logged_in=20 ws_opened_warmup=20`). 90 s clean baseline, then 100 ms of
latency added to every DB round-trip (20 s with latency alone: still 0 pool timeouts), then the trigger, then 6 min
of observation with the trigger over.

| image | revision | trigger | pool timeouts | socket reconnects after trigger | windows >2 min after trigger with timeouts | verdict |
|---|---|---|---|---|---|---|
| `v0.13.3` | `b0e6098` | PostgreSQL paused 45 s | **347** | **214** (20 tabs) | **16 / 16** | **REPRODUCED** |
| `dev` | `2547992` | PostgreSQL paused 45 s | **602** | **205** (20 tabs) | **17 / 17** | **REPRODUCED** |
| `v0.13.3` | `b0e6098` | Redis removed 60 s, recreated empty | 0 | 0 | 0 / 17 | not reproduced at this scale |
| `dev` | `2547992` | Redis removed 60 s, recreated empty | 0 | 0 | 0 / 17 | not reproduced at this scale |

## What the loop looks like (v0.13.3, db-stall)

Server side, every 15 s window, long after PostgreSQL was unpaused at 10:56:28:

```
10:57:14 pool_timeouts=29  10:57:30 pool_timeouts=14  10:57:45 pool_timeouts=18  10:58:01 pool_timeouts=15
10:58:17 pool_timeouts=15  ...  11:02:14 pool_timeouts=14  11:02:30 pool_timeouts=14
```

Browser side (all 20 tabs together, 10 s windows): `ws_new` = new WebSocket connections = reconnects.
Every reconnect is followed by `GET /api/version` and two `/api/v1/chats/...` requests, and the 5xx column is the
DB pool timing out on those requests.

```
      ts    ws_new   version     chats   folders   http_5xx
10:57:17        10        10        20         0        25
10:57:47         7         7        14         2        17
10:59:27        11        11        22         0        15
11:02:27         5         5        10         0        10
```

Stack trace repeated in the server log: the socket **connect handler itself** waits for a DB connection to
verify the token and times out, so the connection is rejected/disconnected and the browser immediately connects
again:

```
File "/app/backend/open_webui/socket/main.py", line 626, in connect
    user = await get_verified_user_by_token(auth['token'], redis)
...
    if decoded is None or 'id' not in decoded or not await is_valid_token(decoded, redis):
sqlalchemy.exc.TimeoutError: QueuePool limit of size 2 overflow 0 reached, connection timed out, timeout 5.00
```

## Reading

- A 45 s database stall is enough to start a loop that does not end on its own: six minutes after the database is
  back, 20 idle tabs still reconnect ~5 times per 10 s and keep the pool saturated. With thousands of tabs this is
  the production outage.
- Losing Redis did not start the loop in this small setup. In production it did, most likely because the Redis loss
  itself produced the first DB slowdown (every request lost its Redis-backed state at once); the loop that followed
  is the same one shown by the `db-stall` runs.
