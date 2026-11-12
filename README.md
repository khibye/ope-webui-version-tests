# Open WebUI reconnect-storm reproduction

Reproduces, on a laptop, the production incident where losing Redis (or a short DB stall)
under load turns into a self-sustaining loop: server disconnects sockets -> browsers reconnect
immediately -> each reconnect issues several DB-backed requests -> DB pool exhausted ->
socket re-verification fails on pool timeout -> server disconnects again.

The DB pool is made tiny on purpose (`DATABASE_POOL_SIZE=2`, no overflow, 5 s timeout) so that
~20 browser tabs are enough. Everything else matches a production topology:
PostgreSQL + Redis + `WEBSOCKET_MANAGER=redis`.

## Requirements
- Docker with Compose v2, ~4 GB free RAM
- Python 3.9+ with Playwright:
  ```bash
  pip install playwright
  python3 -m playwright install chromium        # on Linux add: --with-deps
  ```

## Run
```bash
chmod +x run_check.sh

# latest release, Redis recreated under load (the Nov 12 scenario)
./run_check.sh v0.13.3 redis

# same on current dev
./run_check.sh dev redis

# other triggers
./run_check.sh v0.13.3 redis-oom    # Redis refuses writes for 60 s (the Nov 5 scenario)
./run_check.sh v0.13.3 db-stall     # PostgreSQL paused for 45 s (DB feedback loop alone)
```
Each run takes ~10 minutes and leaves the stack running; stop it with `docker compose down -v`.

From the trigger on, `DB_LATENCY_MS` (default 100) of latency is added to every DB round-trip through a
toxiproxy container, to stand in for a loaded production database; in `redis` mode Redis stays gone for
`REDIS_DOWN_S` (default 60) seconds before it is recreated empty.

Tunables (environment variables): `USERS=5 TABS=4 POOL_SIZE=2 POOL_OVERFLOW=0 POOL_TIMEOUT=5 OBSERVE_S=360 DB_LATENCY_MS=100 REDIS_DOWN_S=60`.
If the baseline already shows pool timeouts, re-run with `POOL_SIZE=5`.

## What to look at
`results/<tag>-<mode>-<time>/`:
- `run.log` - phases and the final **VERDICT**
- `observe.log` - every 15 s: new `QueuePool limit` errors, `Session not found`, `Cannot publish to redis`, live PostgreSQL connections
- `tabs_stats.csv` / `tabs.log` - every 10 s, from the browser side: new WebSocket connections (`ws_new` = reconnects), `/api/version`, `/api/v1/chats/*`, `/api/v1/folders/`, 401/5xx counts
- `open-webui.log`, `redis.log` - full container logs with timestamps
- `version.json` - exact version reported by the instance

Expected healthy behaviour: a burst of reconnects and maybe a few pool timeouts right after the
trigger, then silence within a minute. The bug: `ws_new` and `pool_timeouts` keep climbing in
every window for the whole observation period, long after Redis/PostgreSQL are healthy again.

## Manual variant
```bash
OWUI_TAG=v0.13.3 docker compose up -d
python3 setup_users.py --users 5
python3 open_tabs.py --users 5 --tabs-per-user 4 --duration 900     # leave running
docker compose rm -sf redis && docker compose up -d redis            # trigger
docker compose logs -f open-webui | grep -E "QueuePool|Session not found|Cannot publish"
```
