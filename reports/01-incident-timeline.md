# 01 - Production incident timeline

Deployment: Open WebUI **v0.12.5** on Kubernetes, PostgreSQL, Redis with `WEBSOCKET_MANAGER=redis`,
~50,000 users. Upgraded to 0.12.5 roughly 1.5 weeks before the incident; ran fine until Nov 5.

| Date (2026) | Observation | Interpretation |
|---|---|---|
| Nov 5 | Redis: `OOM command not allowed when used memory > 'maxmemory'` | Redis hit its memory limit and rejected writes. First infrastructure fault. |
| Nov 7 | App logs flooded with `KeyError: 'Session not found'` and `Cannot publish to redis` | Socket layer in distress: sockets dropping and reconnecting in waves. |
| Nov 8 | 3 client IPs loop on `GET /api/v1/chats/pinned` -> 401, ~5.8M requests/day, no pool errors yet | Tabs with invalid tokens stuck in a reconnect/401 loop. Cheap per request, so it ran unnoticed. |
| Nov 10 | `QueuePool limit ... reached, connection timed out` at hundreds/sec; ~2.5M pool timeouts; UI down for everyone | The self-sustaining reconnect loop (see 02) started during business-hours load and never ended. |
| Nov 10-11 | 12 x Redis `Connection closed by server`, timeouts | Consequence of the overloaded app not draining its Redis connections, not a cause. |
| Nov 11 | Revoked sessions of every user active in the last 7 days (`POST /api/v1/users/{id}/sessions/revoke`, `tools/revoke_sessions.py`) | Loop stopped within minutes. Tabs with now-invalid tokens get 401 and are sent to the login page. |
| Nov 11 | `WEBSOCKET_REDIS_OPTIONS` set (`socket_connect_timeout`, `health_check_interval`, ...) | Added after the incident started; not a cause. Harmless; `socket_timeout` not recommended. |
| Nov 12 | Redis pods recreated (adding a Prometheus exporter sidecar) -> immediately the same cascade: `QueuePool`, DB timeouts | **Reproducible in production**: losing Redis under load restarts the loop. |
| Nov 12, later | Many 401 responses, app CPU > 100 %, UI slow | Second-order effect of the revoke: tabs whose sign-out request failed keep looping with invalid tokens. Each 401 still costs a DB lookup plus CPU. |

## What stopped it, and the cost

Revoking all active sessions converts every open tab from "valid token, heavy reconnect loop" into
"invalid token, 401". Most tabs then sign out and stop. Tabs whose sign-out check (`GET /api/v1/auths/`)
fails under load do **not** sign out and keep looping with 401s (the Nov 8 pattern, now at scale). So the revoke
trades a DB-killing loop for a CPU-eating one that decays as people close tabs.

## Open questions about the first trigger

The reproduction (03) shows a short PostgreSQL stall is sufficient to start the loop and Redis loss alone is not,
at small scale. In production the Nov 12 Redis recreation started it immediately. Most likely the Redis loss caused
the first DB slowdown (all per-request Redis-backed state gone at once -> everything falls through to the DB), after
which the loop in 02 sustained itself. Things still worth checking in production:

- PostgreSQL metrics/logs around Nov 10 08:00-10:00 and Nov 12 (checkpoints, autovacuum on `chat`, lock waits,
  failover/maintenance, `max_connections`).
- Size of the `chat` table: `SELECT pg_size_pretty(pg_total_relation_size('chat'));` (0.12.x loads the whole chat
  for every access check; 0.13.1+ no longer does, PR #33667).
