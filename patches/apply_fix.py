#!/usr/bin/env python3
"""
Apply the socket reconnect-storm fix to an Open WebUI source tree.

    python3 apply_fix.py /path/to/open-webui

Works on v0.12.5, v0.13.3 and dev (2026-11-12). Every edit is anchored on exact text and the script fails loudly if an
anchor is missing, so it never half-applies. See ../reports/02-code-analysis.md for why each change exists.
"""
import sys
from pathlib import Path


def edit(path: Path, old: str, new: str, count: int = 1):
    s = path.read_text()
    if s.count(old) != count:
        sys.exit(f"anchor not found ({s.count(old)} matches, expected {count}) in {path}:\n{old[:200]}")
    path.write_text(s.replace(old, new))


def main():
    root = Path(sys.argv[1]).resolve()
    server = root / "backend/open_webui/socket/main.py"
    layout = root / "src/routes/+layout.svelte"
    sidebar = root / "src/lib/components/layout/Sidebar.svelte"
    for p in (server, layout, sidebar):
        if not p.exists():
            sys.exit(f"missing: {p}")

    # ------------------------------------------------------------------ server
    edit(
        server,
        "from redis.exceptions import RedisError\nfrom socketio.packet import Packet\n",
        "import os\n"
        "from redis.exceptions import RedisError\n"
        "from socketio.exceptions import ConnectionRefusedError as SocketConnectionRefusedError\n"
        "from socketio.packet import Packet\n"
        "from sqlalchemy.exc import SQLAlchemyError\n"
        "from starlette.exceptions import HTTPException as StarletteHTTPException\n",
    )

    # Helper + constants, right after the LOCAL_AUTHENTICATED_SIDS declaration
    edit(
        server,
        "LOCAL_AUTHENTICATED_SIDS: set[str] = set()\n",
        '''LOCAL_AUTHENTICATED_SIDS: set[str] = set()

# Errors that mean "the backend could not answer right now", as opposed to "the token is invalid".
# A socket must never be disconnected because of one of these: disconnecting makes every client reconnect
# at once, each reconnect needs the same backend to verify its token, and the resulting load keeps the
# backend unavailable (self-sustaining reconnect storm).
TRANSIENT_BACKEND_ERRORS = (SQLAlchemyError, RedisError, asyncio.TimeoutError, TimeoutError, ConnectionError)
SOCKET_REAUTH_INTERVAL = max(int(os.getenv('SOCKET_REAUTH_INTERVAL', '30')), 10)
SOCKET_REAUTH_JITTER = 15
# A single re-verification must not wait DATABASE_POOL_TIMEOUT for a connection: a sweep over hundreds of sockets
# with a saturated pool would then run for hours. After this many seconds the check counts as "could not verify".
SOCKET_REAUTH_CHECK_TIMEOUT = max(float(os.getenv('SOCKET_REAUTH_CHECK_TIMEOUT', '5')), 1.0)
# Heartbeats arrive every 30 s per socket; writing user.last_active_at on each one is one UPDATE + commit per socket
# per 30 s. Write it at most this often per socket instead.
HEARTBEAT_LAST_ACTIVE_WRITE_INTERVAL = max(int(os.getenv('HEARTBEAT_LAST_ACTIVE_WRITE_INTERVAL', '300')), 30)


def is_transient_backend_error(exc: BaseException) -> bool:
    if isinstance(exc, StarletteHTTPException):
        # get_auth() maps database errors to 503 ("Authentication is temporarily unavailable.")
        return exc.status_code in (502, 503, 504)
    return isinstance(exc, TRANSIENT_BACKEND_ERRORS)
''',
    )

    # Jitter on the periodic sweep (the rest of that function is version-specific and left untouched)
    edit(
        server,
        "async def periodic_socket_authentication():\n    while True:\n        await asyncio.sleep(30)\n",
        "async def periodic_socket_authentication():\n    while True:\n"
        "        # Jitter so that workers do not all hit the database in the same second.\n"
        "        await asyncio.sleep(SOCKET_REAUTH_INTERVAL + random.uniform(0, SOCKET_REAUTH_JITTER))\n"
        "        sweep_started = time.monotonic()\n",
    )
    edit(
        server,
        "        for sid in tuple(LOCAL_AUTHENTICATED_SIDS):\n"
        "            session_user = await get_socket_session_user(sid)\n"
        if "session_user = await get_socket_session_user(sid)" in server.read_text()
        else "        for sid in tuple(LOCAL_AUTHENTICATED_SIDS):\n            await get_socket_session_user(sid)\n",
        (
            "        for i, sid in enumerate(tuple(LOCAL_AUTHENTICATED_SIDS)):\n"
            "            if i % 50 == 49:\n"
            "                await asyncio.sleep(0)  # let heartbeats and requests run between checks\n"
            "            session_user = await get_socket_session_user(sid)\n"
            if "session_user = await get_socket_session_user(sid)" in server.read_text()
            else
            "        for i, sid in enumerate(tuple(LOCAL_AUTHENTICATED_SIDS)):\n"
            "            if i % 50 == 49:\n"
            "                await asyncio.sleep(0)  # let heartbeats and requests run between checks\n"
            "            await get_socket_session_user(sid)\n"
        ),
    )
    edit(
        server,
        "\n\nasync def get_socket_session_user(",
        "\n        sweep_took = time.monotonic() - sweep_started\n"
        "        if sweep_took > SOCKET_REAUTH_INTERVAL:\n"
        "            log.warning('Socket re-verification sweep took %.0fs for %d sockets (interval %ds)',\n"
        "                        sweep_took, len(LOCAL_AUTHENTICATED_SIDS), SOCKET_REAUTH_INTERVAL)\n"
        "\n\nasync def get_socket_session_user(",
    )

    # Replace get_socket_session_user() entirely (identical in v0.12.5, v0.13.3 and dev)
    s = server.read_text()
    start = s.index("async def get_socket_session_user(")
    end = s.index("encryption = Relay(sio, get_socket_session_user, REDIS)")
    new_block = '''async def get_socket_session_user(sid: str, *, wait_for_disconnect: bool = True) -> dict | None:
    """Session user from this worker's local Socket.IO store; only locally connected sids are ever looked up.

    Disconnects the socket only when the token is actually invalid. If the backend cannot be reached to
    re-verify it, the previously verified session user is kept and the check is retried on the next sweep.
    """
    session = None
    try:
        session = await sio.get_session(sid)
    except Exception:
        log.debug('No local session for %s', sid)

    if session and session.get('user'):
        try:
            if await asyncio.wait_for(
                get_verified_user_by_token(session.get('token', ''), REDIS), timeout=SOCKET_REAUTH_CHECK_TIMEOUT
            ):
                return session['user']
            log.debug('Socket authentication expired for %s', sid)
        except Exception as e:
            if is_transient_backend_error(e):
                log.warning(
                    'Could not re-verify socket %s because the backend is unavailable (%s); keeping the session',
                    sid,
                    e,
                )
                return session['user']
            log.debug('Socket authentication check failed for %s', sid, exc_info=True)

    if not (session and session.get('user')):
        # Never authenticated (login page, or a tab whose token was cleared by another tab): ignore the event
        # and keep the socket. Disconnecting it makes clients reconnect at once and re-send the same event
        # (e.g. the chat page's events:chat join on every connect): a reconnect loop of many per second.
        return None

    LOCAL_AUTHENTICATED_SIDS.discard(sid)
    if wait_for_disconnect:
        await sio.disconnect(sid)
    else:
        # Document handlers hold a lock that disconnect cleanup also needs.
        sio.start_background_task(sio.disconnect, sid)
    return None


'''
    server.write_text(s[:start] + new_block + s[end:])

    edit(
        server,
        "        redis = getattr(getattr(fastapi_app, 'state', None), 'redis', None) or REDIS\n"
        "        user = await get_verified_user_by_token(auth['token'], redis)\n\n"
        "        if user:\n",
        "        redis = getattr(getattr(fastapi_app, 'state', None), 'redis', None) or REDIS\n"
        "        try:\n"
        "            user = await get_verified_user_by_token(auth['token'], redis)\n"
        "        except Exception as e:\n"
        "            if not is_transient_backend_error(e):\n"
        "                raise\n"
        "            # Refuse the handshake with an explicit reason instead of letting the exception tear down the\n"
        "            # transport. The client sees a connect_error carrying this data and retries with backoff.\n"
        "            log.warning('Refusing socket %s: backend unavailable during token verification (%s)', sid, e)\n"
        "            raise SocketConnectionRefusedError('backend_unavailable', {'retry': True}) from None\n\n"
        "        if user is None and auth['token']:\n"
        "            # The token was supplied but is invalid (expired or revoked). Refuse the handshake instead of\n"
        "            # accepting an anonymous socket that every heartbeat would then disconnect, which makes the\n"
        "            # client reconnect and re-fetch the whole sidebar in a loop (a 401 storm after a mass revoke).\n"
        "            log.debug('Refusing socket %s: invalid token', sid)\n"
        "            raise SocketConnectionRefusedError('unauthorized')\n\n"
        "        if user:\n",
    )

    # --- server: cheap heartbeat. The periodic sweep already re-verifies every socket against the database, so a
    # heartbeat only refreshes last_seen in Redis and writes user.last_active_at every few minutes instead of
    # (JWT decode + 2 DB reads + 1 DB write + commit) per socket per 30 s.
    s = server.read_text()
    hb_start = s.index("@sio.on('heartbeat')\nasync def heartbeat(sid, data):\n")
    hb_end = s.index("\n\n@sio.on(", hb_start)
    hb_old = s[hb_start:hb_end]
    if "await Users.update_last_active_by_id(user['id'])" not in hb_old:
        sys.exit("heartbeat handler looks different than expected")
    hb_new = '''@sio.on('heartbeat')
async def heartbeat(sid, data):
    try:
        session = await sio.get_session(sid)
    except Exception:
        session = None
    if not session or not session.get('user'):
        # Anonymous socket (login page): nothing to refresh, and disconnecting it would only make the client
        # reconnect and re-fetch everything.
        return
    user = session['user']
    now = int(time.time())
    try:
        SESSION_POOL[sid] = {
            **(SESSION_POOL.get(sid) or {}),
            **user,
            'last_seen_at': now,
        }
    except Exception as e:
        log.debug('heartbeat: could not refresh session pool for %s: %s', sid, e)
    if now - session.get('last_active_written_at', 0) >= HEARTBEAT_LAST_ACTIVE_WRITE_INTERVAL:
        try:
            await Users.update_last_active_by_id(user['id'])
            session['last_active_written_at'] = now
            await sio.save_session(sid, session)
        except Exception as e:
            if not is_transient_backend_error(e):
                raise
            log.debug('heartbeat: could not update last_active_at for %s: %s', sid, e)'''
    server.write_text(s[:hb_start] + hb_new + s[hb_end:])
    # user-join only (join_channel / join_note contain the same lines and are left as they are)
    s = server.read_text()
    fn_start = s.index("async def user_join(sid, data):")
    fn_end = s.index("\n@sio.", fn_start)
    fn = s[fn_start:fn_end]
    old_snip = (
        "    user = await get_verified_user_by_token(auth['token'], redis)\n"
        "    if not user:\n"
        "        return\n"
    )
    new_snip = (
        "    try:\n"
        "        user = await get_verified_user_by_token(auth['token'], redis)\n"
        "    except Exception as e:\n"
        "        if not is_transient_backend_error(e):\n"
        "            raise\n"
        "        log.warning('user-join for %s could not be verified: backend unavailable (%s)', sid, e)\n"
        "        return {'error': 'backend_unavailable', 'retry': True}\n"
        "    if not user:\n"
        "        return\n"
    )
    if fn.count(old_snip) != 1:
        sys.exit("user_join anchor not found")
    server.write_text(s[:fn_start] + fn.replace(old_snip, new_snip) + s[fn_end:])

    # --- connect: refuse only an INVALID token. A valid session whose user is not a verified role (for example
    # 'pending', the default role for new SSO users) must not be refused as 'unauthorized': the client would sign
    # the user out. get_verified_user_by_token() returns None for both cases, so check the token and the role
    # separately (same two queries as before).
    edit(
        server,
        "from open_webui.utils.auth import get_verified_user_by_token\n",
        "from open_webui.utils.auth import get_verified_user_by_token\n"
        "from open_webui.utils.auth import decode_token, get_verified_user_by_id, is_valid_token\n",
    )
    edit(
        server,
        "        try:\n"
        "            user = await get_verified_user_by_token(auth['token'], redis)\n"
        "        except Exception as e:\n",
        "        token_valid = False\n"
        "        try:\n"
        "            decoded = decode_token(auth['token']) if auth['token'] else None\n"
        "            if decoded is not None and 'id' in decoded:\n"
        "                token_valid = await is_valid_token(decoded, redis)\n"
        "            # A valid session whose role is not verified (e.g. 'pending') stays an anonymous socket, as before.\n"
        "            user = await get_verified_user_by_id(decoded['id']) if token_valid else None\n"
        "        except Exception as e:\n",
    )
    edit(
        server,
        "        if user is None and auth['token']:\n",
        "        if not token_valid and auth['token']:\n",
    )

    # ------------------------------------------------------------------ client: +layout.svelte
    edit(
        layout,
        "\t\tawait socket.set(_socket);\n\n"
        "\t\t_socket.on('connect_error', (err) => {\n"
        "\t\t\tconsole.log('connect_error', err);\n"
        "\t\t});\n\n"
        "\t\tlet hasConnectedOnce = false;\n\n"
        "\t\t_socket.on('connect', async () => {\n"
        "\t\t\tconsole.log('connected', _socket.id);\n",
        "\t\tawait socket.set(_socket);\n\n"
        "\t\t// Reconnect with exponential backoff and jitter instead of immediately. An immediate reconnect from\n"
        "\t\t// thousands of tabs at once is what turns a short backend hiccup into a self-sustaining storm.\n"
        "\t\tlet reconnectAttempt = 0;\n"
        "\t\tlet reconnectTimer = null;\n"
        "\t\tconst RECONNECT_BASE_MS = 1000;\n"
        "\t\tconst RECONNECT_MAX_MS = 30000;\n"
        "\t\tconst scheduleReconnect = (why) => {\n"
        "\t\t\tif (reconnectTimer || _socket.connected) {\n"
        "\t\t\t\treturn;\n"
        "\t\t\t}\n"
        "\t\t\tconst base = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** reconnectAttempt);\n"
        "\t\t\tconst delay = Math.round(base * (0.5 + Math.random())); // 50%..150% of base\n"
        "\t\t\treconnectAttempt += 1;\n"
        "\t\t\tconsole.log(`Socket reconnect (${why}) in ${delay}ms, attempt ${reconnectAttempt}`);\n"
        "\t\t\treconnectTimer = setTimeout(() => {\n"
        "\t\t\t\treconnectTimer = null;\n"
        "\t\t\t\tif (!_socket.connected) {\n"
        "\t\t\t\t\t_socket.connect();\n"
        "\t\t\t\t}\n"
        "\t\t\t}, delay);\n"
        "\t\t};\n\n"
        "\t\t_socket.on('connect_error', (err) => {\n"
        "\t\t\tconsole.log('connect_error', err);\n"
        "\t\t\tif (err?.message === 'unauthorized') {\n"
        "\t\t\t\t// The server refused our token (expired or revoked). Sign out right here instead of relying on a\n"
        "\t\t\t\t// later 401 + confirmation request, which fails under load and leaves the tab looping.\n"
        "\t\t\t\tconsole.log('Socket refused: session is no longer valid, signing out');\n"
        "\t\t\t\tclearExpiredSession();\n"
        "\t\t\t\t// `auth` re-reads localStorage.token, which is now gone: the next connect is anonymous and\n"
        "\t\t\t\t// accepted, so the login page still has a socket to emit user-join on after signing in.\n"
        "\t\t\t\tscheduleReconnect('unauthorized');\n"
        "\t\t\t\treturn;\n"
        "\t\t\t}\n"
        "\t\t\t// `active` is false when the server refused the handshake (e.g. backend temporarily unavailable):\n"
        "\t\t\t// socket.io does not retry those on its own, so retry here with backoff.\n"
        "\t\t\tif (!_socket.active) {\n"
        "\t\t\t\tscheduleReconnect('connect_error');\n"
        "\t\t\t}\n"
        "\t\t});\n\n"
        "\t\tlet hasConnectedOnce = false;\n\n"
        "\t\t_socket.on('connect', async () => {\n"
        "\t\t\tconsole.log('connected', _socket.id);\n"
        "\t\t\treconnectAttempt = 0;\n"
        "\t\t\tif (reconnectTimer) {\n"
        "\t\t\t\tclearTimeout(reconnectTimer);\n"
        "\t\t\t\treconnectTimer = null;\n"
        "\t\t\t}\n",
    )
    edit(
        layout,
        "\t\twindow.addEventListener('message', windowMessageEventHandler);\n",
        "\t\twindow.addEventListener('message', windowMessageEventHandler);\n"
        "\t\t// When another tab of this user signs out (token removed from localStorage), follow it instead of\n"
        "\t\t// lingering with a stale session and an anonymous socket.\n"
        "\t\twindow.addEventListener('storage', (e) => {\n"
        "\t\t\tif (e.key === 'token' && !e.newValue && $user) {\n"
        "\t\t\t\tconsole.log('Token removed by another tab, signing out');\n"
        "\t\t\t\tclearExpiredSession();\n"
        "\t\t\t}\n"
        "\t\t});\n",
    )
    edit(
        layout,
        "\t\t\tif (reason === 'io server disconnect') {\n\t\t\t\t_socket.connect();\n\t\t\t}\n",
        "\t\t\tif (reason === 'io server disconnect') {\n\t\t\t\tscheduleReconnect('server disconnect');\n\t\t\t}\n",
    )
    edit(
        layout,
        "\t\t\t\t_socket.emit('user-join', { auth: { token: localStorage.token } });\n",
        "\t\t\t\t// Retry later if the backend could not verify the token right now\n"
        "\t\t\t\tconst emitUserJoin = (attempt = 0) => {\n"
        "\t\t\t\t\t_socket.emit('user-join', { auth: { token: localStorage.token } }, (res) => {\n"
        "\t\t\t\t\t\tif (res?.error === 'backend_unavailable' && res?.retry && attempt < 6 && _socket.connected) {\n"
        "\t\t\t\t\t\t\tconst delay = Math.round(Math.min(30000, 2000 * 2 ** attempt) * (0.5 + Math.random()));\n"
        "\t\t\t\t\t\t\tconsole.log(`user-join deferred by server, retrying in ${delay}ms`);\n"
        "\t\t\t\t\t\t\tsetTimeout(() => emitUserJoin(attempt + 1), delay);\n"
        "\t\t\t\t\t\t}\n"
        "\t\t\t\t\t});\n"
        "\t\t\t\t};\n"
        "\t\t\t\temitUserJoin();\n",
    )

    # ------------------------------------------------------------------ client: Sidebar.svelte
    edit(
        sidebar,
        "\tconst refreshChatRows = async () => {\n",
        "\t// Each socket reconnect re-fetches the chat list, pinned chats and folders. During a reconnect storm that\n"
        "\t// is several DB-backed requests per tab per reconnect, so skip refreshes that follow each other closely.\n"
        "\tconst RECONNECT_REFRESH_MIN_INTERVAL_MS = 15000;\n"
        "\tlet lastChatRowsRefreshAt = 0;\n"
        "\tconst refreshChatRows = async () => {\n"
        "\t\tconst now = Date.now();\n"
        "\t\tif (chatListReady && now - lastChatRowsRefreshAt < RECONNECT_REFRESH_MIN_INTERVAL_MS) {\n"
        "\t\t\treturn;\n"
        "\t\t}\n"
        "\t\tlastChatRowsRefreshAt = now;\n",
    )
    print(f"fix applied to {root}")


if __name__ == "__main__":
    main()
