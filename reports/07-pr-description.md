# PR description (target branch: `dev`) - use only if a maintainer asks for a PR

**Title:** fix(socket): stop self-sustaining reconnect storms after a backend hiccup

Fixes #[issue number].

## What this changes

A short DB/Redis stall under load turned into an outage that did not end when the backend recovered (details,
line references and reproduction in the issue). Two loops:

1. **valid tokens**: transient DB error in token re-verification -> socket disconnected -> immediate client
   reconnect -> full sidebar refresh -> pool saturated -> next verification fails -> repeat.
2. **revoked tokens** (after "Sign out all devices"): invalid token accepted as an anonymous socket -> every heartbeat
   disconnects it -> reconnect + sidebar refresh -> 401 x3 -> repeat, because the client's sign-out path needs a
   confirmation request that fails under load.

### Server (`backend/open_webui/socket/main.py`)
- `is_transient_backend_error()`: SQLAlchemy / Redis / timeout / connection errors and `get_auth()`'s 503 mean
  "could not verify", not "invalid token".
- `get_socket_session_user()`: on a transient error keep the previously verified session user and retry on the next
  sweep; disconnect only on a real invalid token. Each check is bounded by `SOCKET_REAUTH_CHECK_TIMEOUT` (5 s).
- `connect()`: refuse with `ConnectionRefusedError('backend_unavailable', {retry: true})` on a transient error and
  with `'unauthorized'` when a supplied token is invalid, instead of letting the exception tear down the transport or
  accepting an anonymous socket.
- `heartbeat`: never disconnect anonymous sockets; no DB re-verification (the sweep does it); write
  `user.last_active_at` at most every `HEARTBEAT_LAST_ACTIVE_WRITE_INTERVAL` (300 s).
- `periodic_socket_authentication()`: 0-15 s jitter, `SOCKET_REAUTH_INTERVAL` env (default 30), yields every 50
  sockets, warns when a sweep overruns the interval.
- `user-join`: returns `{error: 'backend_unavailable', retry: true}` instead of failing silently.

### Client
- `src/routes/+layout.svelte`: exponential backoff with jitter (1 s .. 30 s, reset on connect) before reconnecting
  after `io server disconnect` or a refused handshake; on `unauthorized` clear the session and go to `/auth`,
  then reconnect anonymously so the login page keeps a socket; retry `user-join` with backoff on
  `backend_unavailable`; sibling tabs follow a sign-out (`storage` event).
- `src/lib/components/layout/Sidebar.svelte`: skip reconnect-triggered full refreshes within 15 s.

## Behaviour changes to be aware of
- An already-open socket keeps its previously verified user while the backend is unreachable, for at most one sweep
  interval (<= ~45 s longer than today). HTTP requests still fail closed.
- `last_active_at` is updated every 5 min per socket instead of every 30 s.
- A refused handshake (`unauthorized`) is not retried by old clients that have not reloaded; they stay quiet until
  reload instead of looping.

## Verification
Automated reproduction (20 Playwright tabs, PostgreSQL behind toxiproxy, tiny pool): PostgreSQL paused 45 s,
Redis removed 60 s, all sessions revoked - on v0.12.5, v0.13.3 and dev, unpatched vs patched.
https://github.com/khibye/ope-webui-version-tests (reports 03 and 06, workflow runs linked there).
Unpatched: pool timeouts and reconnects continue for the whole 6-minute observation. Patched: none after the trigger;
revoked tabs reach the login page and go quiet. Running in production (v0.12.5 + this patch) since 2026-11-16.

## Not covered
Multi-worker / multi-pod deployments were not part of the automated test (production runs 9 single-worker pods
with Redis Cluster and shows the expected behaviour). No schema or migration changes.
