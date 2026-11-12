# 04 - Mitigations and operational recommendations

Ordered by how directly each one breaks the loop described in 02. None of them fixes the bug; the fix is upstream.

## Stop an active loop

| Action | Effect | Caveat |
|---|---|---|
| Admin Panel -> Users -> user -> **Sign out all devices** (`POST /api/v1/users/{id}/sessions/revoke`) | Invalidates every token of that user; their looping tabs get 401 and (usually) land on the login page | Need to know which user. Find the hot IP in the access log, or the hot chat id in `/api/tasks/chat/<id>` requests and map it to `chat.user_id`. |
| `tools/revoke_sessions.py --days 7 --workers 10` | Same thing for every user active in the last N days, in parallel (50k users -> minutes) | Creates the second loop (invalid-token 401s) for tabs whose sign-out check fails under load. Run it once, in a quiet hour if possible, and expect elevated 401s/CPU afterwards that decay as people close tabs. |
| Rotating `WEBUI_SECRET_KEY` | Invalidates all tokens at JWT level (no DB lookup) | **Not recommended**: the key also encrypts MFA secrets, tool/function valves, OAuth and MCP sessions. |

## Cap the storm before it reaches the app (circuit breaker)

Rate-limit the endpoints a reconnect fires, per client, at the ingress. Legitimate use never comes close; a looping
tab is cut off with 429 at nginx, costing the app no CPU and no DB connection.

```yaml
# second Ingress object with the same host; annotations apply only to its own paths
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: open-webui-loop-guard
  annotations:
    nginx.ingress.kubernetes.io/limit-rps: "3"
    nginx.ingress.kubernetes.io/limit-burst-multiplier: "5"
spec:
  ingressClassName: nginx
  rules:
    - host: chat.example.com
      http:
        paths:
          - { path: /api/version,        pathType: Exact, backend: { service: { name: open-webui, port: { number: 8080 } } } }
          - { path: /api/v1/chats/,      pathType: Exact, backend: { service: { name: open-webui, port: { number: 8080 } } } }
          - { path: /api/v1/chats/pinned, pathType: Exact, backend: { service: { name: open-webui, port: { number: 8080 } } } }
          - { path: /api/v1/folders/,    pathType: Exact, backend: { service: { name: open-webui, port: { number: 8080 } } } }
```
Plus `limit-req-status-code: "429"` in the controller ConfigMap. Check that the ingress sees real client IPs
(`use-forwarded-headers` / `externalTrafficPolicy: Local`), and raise the limit if many users share a NAT address.
A single emergency alternative on the existing Ingress: `nginx.ingress.kubernetes.io/denylist-source-range` with the
offending IPs.

## Make a transient stall survivable (so the loop never starts)

- **PgBouncer** in transaction mode in front of PostgreSQL. Lets the app open many connections while PostgreSQL sees
  few; a reconnect burst becomes a queue instead of a collapse.
- **Pool headroom**: `DATABASE_POOL_SIZE=20`, `DATABASE_POOL_MAX_OVERFLOW=20` (per pod x workers must stay under
  PostgreSQL `max_connections`, or go through PgBouncer). This raises the threshold; it does not remove the loop.
- **Upgrade to 0.13.1+**: chat access checks no longer load the whole chat (PR #33667), so each loop iteration is
  cheaper. 0.13.0 has DB schema changes: back up first, upgrade all replicas together.
- **CPU limits**: an app pod throttled at its CPU limit slows the very request (`GET /api/v1/auths/`) that lets a
  stuck tab sign out. Prefer raising the CPU limit over adding pods (pods multiply DB connections).

## Keep Redis from disappearing

- Run the Prometheus `redis-exporter` as a **separate Deployment** pointing at the Redis Service, not as a sidecar
  (a sidecar change recreates the Redis pod - that is what retriggered the incident on Nov 12).
- Redis with replicas + Sentinel and a `PodDisruptionBudget` (`minAvailable: 1`); Open WebUI supports
  `REDIS_SENTINEL_HOSTS` / `WEBSOCKET_SENTINEL_HOSTS`. Rolling restarts then always keep a master.
- Persistence (`appendonly yes` on a PVC) so a restarted Redis comes back with the socket session pool intact.
- `maxmemory` with real headroom (the Nov 5 OOM was the first fault). Policy: `volatile-lru` at most; never
  `allkeys-lru`, which can evict `session_pool` and break every socket at once.
- Alerts: `redis_memory_used_bytes / redis_memory_max_bytes > 0.8`, `redis_up == 0`,
  `redis_rejected_connections_total`, PostgreSQL connections vs `max_connections`, app 5xx rate,
  `QueuePool limit` log rate.
- Any Redis change, including "just monitoring", in a quiet hour: the storm size is proportional to open tabs.

## `WEBSOCKET_REDIS_OPTIONS`

Only applies when `WEBSOCKET_MANAGER=redis`; passed as `redis_options` to socket.io's `AsyncRedisManager`.
Recommended: `{"socket_connect_timeout": 5, "health_check_interval": 30}`. Drop `socket_timeout` (the app
defaults it to `null` on purpose; a finite value fails commands under load) and `retry_on_timeout` (deprecated and
already the default in redis-py 8). Not related to the loop either way.
