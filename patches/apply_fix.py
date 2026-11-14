#!/usr/bin/env python3
"""
Apply the socket reconnect-storm fix to an Open WebUI source tree.

    python3 apply_fix.py /path/to/open-webui

Works on v0.13.3 and on dev (2026-11-12). Every edit is anchored on exact text and the script fails loudly if an
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
        "from redis.exceptions import RedisError\n"
        "from socketio.exceptions import ConnectionRefusedError as SocketConnectionRefusedError\n"
        "from socketio.packet import Packet\n"
        "from sqlalchemy.exc import SQLAlchemyError\n"
        "from starlette.exceptions import HTTPException as StarletteHTTPException\n",
    )

    s = server.read_text()
    start = s.index("LOCAL_AUTHENTICATED_SIDS: set[str] = set()\n")
    end = s.index("encryption = Relay(sio, get_socket_session_user, REDIS)")
    block = s[start:end]
    assert "async def periodic_socket_authentication" in block and "async def get_socket_session_user" in block
    new_block = '''LOCAL_AUTHENTICATED_SIDS: set[str] = set()

# Errors that mean "the backend could not answer right now", as opposed to "the token is invalid".
# A socket must never be disconnected because of one of these: disconnecting makes every client reconnect
# at once, each reconnect needs the same backend to verify its token, and the resulting load keeps the
# backend unavailable (self-sustaining reconnect storm).
TRANSIENT_BACKEND_ERRORS = (SQLAlchemyError, RedisError, asyncio.TimeoutError, TimeoutError, ConnectionError)
SOCKET_REAUTH_INTERVAL = 30
SOCKET_REAUTH_JITTER = 15


def is_transient_backend_error(exc: BaseException) -> bool:
    if isinstance(exc, StarletteHTTPException):
        # get_auth() maps database errors to 503 ("Authentication is temporarily unavailable.")
        return exc.status_code in (502, 503, 504)
    return isinstance(exc, TRANSIENT_BACKEND_ERRORS)


async def periodic_socket_authentication():
    while True:
        # Jitter so that workers do not all hit the database in the same second.
        await asyncio.sleep(SOCKET_REAUTH_INTERVAL + random.uniform(0, SOCKET_REAUTH_JITTER))
        await encryption.sweep()
        for sid in tuple(LOCAL_AUTHENTICATED_SIDS):
            session_user = await get_socket_session_user(sid)
            if session_user:
                from open_webui.socket.comments import revalidate_comment_subscription

                try:
                    await revalidate_comment_subscription(sio, sid, session_user)
                except Exception:
                    log.exception('Could not revalidate comment subscription')


async def get_socket_session_user(sid: str, *, wait_for_disconnect: bool = True) -> dict | None:
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
            if await get_verified_user_by_token(session.get('token', ''), REDIS):
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
        "        if user:\n",
    )
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
