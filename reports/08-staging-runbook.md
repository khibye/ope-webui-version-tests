# 08 - Runbook: reproducing the incident on a production-like cluster

Goal: start the reconnect storm on purpose, in staging, on the **unpatched** image, then prove the patched image
survives the same trigger. Never on production.

## 0. Preconditions

- Staging namespace with the same shape as production: several Open WebUI pods (1 uvicorn worker each),
  PostgreSQL, Redis (cluster), `WEBSOCKET_MANAGER=redis`, same `DATABASE_POOL_*` values.
- Image for the baseline run: `ghcr.io/open-webui/open-webui:v0.12.5`. For the verification run:
  `ghcr.io/khibye/ope-webui-version-tests/open-webui:v0.12.5-fix-<sha>`.
- An admin token for staging (browser console as admin: `localStorage.token`).
- A load machine with Node 20+ that can reach the staging ingress: `npm install socket.io-client@^4.8`.
- A way to stop everything: Ctrl+C on the simulator closes every socket. Keep `tools/revoke_sessions.py` at hand
  for real browsers you opened.

## 1. Open the "tabs"

The loop needs browser behaviour (sidebar refresh on every connect, immediate reconnect). `tools/socket_storm_sim.mjs`
mimics the 0.12.5 frontend exactly, so one machine stands in for thousands of tabs:

```bash
node tools/socket_storm_sim.mjs --url https://staging.example.com --admin-token "$ADMIN" \
  --users 200 --tabs-per-user 10          # 2,000 sockets; raise until pods sit at ~50% CPU
```
It creates users `repro000..199@example.com` (admin `auths/add`), signs them in, opens the sockets, and prints every
10 s: live sockets, connects, server disconnects, 401 / 5xx counts. Let it run 2-3 minutes: the baseline should show
`server_disconnect=0` and `http_5xx=0` in every window. If it does not, the cluster is already overloaded by the
keepalive cost alone: lower `--users` until the baseline is clean.

Optional: open 5-10 real browser tabs too, to see what users see.

## 2. Watch (second terminal)

```bash
# pool exhaustion rate, every 15 s
watch -n 15 'kubectl logs -n staging -l app=open-webui --since=15s --max-log-requests=20 | grep -c "QueuePool limit"'
# PostgreSQL
psql -c "select state, count(*) from pg_stat_activity where datname=current_database() group by 1"
# pods
kubectl top pods -n staging
```

## 3. Fire a trigger (pick one)

**A. DB stall, deterministic (the minimal trigger).** Block every token verification for 45 s by locking the two
tables it reads, in one psql session:
```sql
BEGIN;
LOCK TABLE auth IN ACCESS EXCLUSIVE MODE;
LOCK TABLE "user" IN ACCESS EXCLUSIVE MODE;
SELECT pg_sleep(45);
COMMIT;
```
Works on managed databases too; nothing to restore afterwards. While it holds, every `connect`, heartbeat and
sweep check waits for a pool connection and times out.

**B. Redis recreated (what happened on Nov 12).**
```bash
kubectl delete pod -n staging -l app.kubernetes.io/name=redis     # or: kubectl rollout restart statefulset/redis -n staging
```
Less deterministic: in the small lab this alone did not start the loop; at cluster scale it did.

**C. Mass revoke (what happened on Nov 15).** Starts loop 2 (invalid tokens) instead of loop 1:
```bash
python3 tools/revoke_sessions.py --days 1 --workers 10      # OWUI_URL / OWUI_TOKEN set to staging
```

## 4. What "reproduced" looks like (unpatched v0.12.5)

- Simulator: `server_disconnect` and `connect` climb in every window and **keep climbing minutes after the
  trigger ended**; `http_5xx` stays high. With trigger C: `http_401` constant (3 per reconnect) and never 0.
- Cluster: `QueuePool limit` keeps appearing every window; `pg_stat_activity` shows few `active` and many
  `idle in transaction`; pod CPU rises.
- Real tabs: sidebar flickers, chats fail to load, no recovery.

Stop it with Ctrl+C on the simulator. If real browser tabs are still looping, revoke their sessions
(`tools/revoke_sessions.py`) or close them.

## 5. Same trigger on the patched image

Roll the deployment to `v0.12.5-fix-<sha>`, restart the simulator (add `--backoff` to mimic the patched frontend;
run it once without `--backoff` too, since old tabs keep the old client until reload), repeat step 3.

Expected: during trigger A the server logs `Could not re-verify socket ... backend unavailable ... keeping the
session` and `server_disconnect` stays 0; within a minute of the lock releasing `QueuePool limit` stops and
`http_5xx` returns to 0. With trigger C: a burst of `http_401` and `connect_error` for a minute, then 0, and
`live` drops to the admin's sockets only (revoked tabs are refused and stop).

## 6. Clean up

Ctrl+C the simulator; delete the `repro*@example.com` users from Admin Panel if staging is shared.
