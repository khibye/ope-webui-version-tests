#!/usr/bin/env node
/**
 * Simulates N open Open WebUI browser tabs the way the 0.12.5 frontend behaves, so a staging
 * cluster can be pushed into the reconnect storm without thousands of real browsers.
 *
 * Per "tab":  socket.io connect with the user's token  ->  on connect: emit user-join, then
 *             GET /api/version, /api/v1/chats/?page=1, /api/v1/chats/pinned, /api/v1/folders/
 *             (exactly what +layout.svelte and Sidebar.svelte do)  ->  heartbeat every 30 s  ->
 *             on 'io server disconnect': reconnect immediately (the unpatched client), or with
 *             backoff when --backoff is given (the patched client).
 *
 * Setup:   npm install socket.io-client@^4.8
 * Usage:   node socket_storm_sim.mjs --url https://chat.example.com --admin-token <token> \
 *              --users 200 --tabs-per-user 10 [--backoff] [--password 'Repro-Pass-1234!']
 * Users repro000..repro(N-1)@example.com are created by the admin if missing (role user), then signed in.
 * Ctrl+C closes every socket: that is also how you end the storm.
 */
import { io } from 'socket.io-client';

const arg = (k, d) => { const i = process.argv.indexOf(k); return i > -1 ? process.argv[i + 1] : d; };
const URL_ = (arg('--url', '') || '').replace(/\/$/, '');
const ADMIN = arg('--admin-token', '');
const USERS = +arg('--users', '50');
const TABS = +arg('--tabs-per-user', '4');
const PASSWORD = arg('--password', 'Repro-Pass-1234!');
const BACKOFF = process.argv.includes('--backoff');
const HEARTBEAT_MS = +arg('--heartbeat-ms', '30000');
const RAMP_MS = +arg('--ramp-ms', '20');       // delay between tab starts
if (!URL_ || !ADMIN) { console.error('need --url and --admin-token'); process.exit(1); }

const stats = { connect: 0, server_disconnect: 0, transport_disconnect: 0, connect_error: 0, http_ok: 0, http_401: 0, http_5xx: 0, http_err: 0 };
const bump = (k) => { stats[k] = (stats[k] || 0) + 1; };

async function api(method, path, token, body) {
  const r = await fetch(URL_ + path, { method, headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) }, body: body ? JSON.stringify(body) : undefined });
  return r;
}

async function ensureUser(i) {
  const email = `repro${String(i).padStart(3, '0')}@example.com`;
  let r = await api('POST', '/api/v1/auths/signin', null, { email, password: PASSWORD });
  if (r.status === 400 || r.status === 401) {
    const a = await api('POST', '/api/v1/auths/add', ADMIN, { name: `Repro ${i}`, email, password: PASSWORD, role: 'user' });
    if (!a.ok && a.status !== 400) throw new Error(`add user ${email}: ${a.status}`);
    r = await api('POST', '/api/v1/auths/signin', null, { email, password: PASSWORD });
  }
  if (!r.ok) throw new Error(`signin ${email}: ${r.status}`);
  return (await r.json()).token;
}

function sidebarRefresh(token) {
  for (const p of ['/api/version', '/api/v1/chats/?page=1', '/api/v1/chats/pinned', '/api/v1/folders/']) {
    api('GET', p, token).then((r) => bump(r.ok ? 'http_ok' : r.status === 401 ? 'http_401' : r.status >= 500 ? 'http_5xx' : 'http_other')).catch(() => bump('http_err'));
  }
}

function tab(token) {
  const s = io(URL_, { path: '/ws/socket.io', transports: ['websocket'], auth: { token }, reconnection: true, reconnectionDelay: 1000, reconnectionDelayMax: 5000, randomizationFactor: 0.5 });
  let attempt = 0, timer = null;
  const reconnect = (why) => {
    if (!BACKOFF) { s.connect(); return; }
    if (timer || s.connected) return;
    const delay = Math.round(Math.min(30000, 1000 * 2 ** attempt) * (0.5 + Math.random()));
    attempt += 1;
    timer = setTimeout(() => { timer = null; if (!s.connected) s.connect(); }, delay);
  };
  s.on('connect', () => { bump('connect'); attempt = 0; s.emit('user-join', { auth: { token } }); sidebarRefresh(token); });
  s.on('disconnect', (reason) => { if (reason === 'io server disconnect') { bump('server_disconnect'); reconnect('server'); } else bump('transport_disconnect'); });
  s.on('connect_error', (e) => { bump('connect_error'); if (!s.active) reconnect('refused'); });
  setInterval(() => { if (s.connected) s.emit('heartbeat', {}); }, HEARTBEAT_MS);
  return s;
}

const sockets = [];
console.log(`creating ${USERS} users x ${TABS} tabs = ${USERS * TABS} sockets against ${URL_} (${BACKOFF ? 'patched client: backoff' : 'unpatched client: immediate reconnect'})`);
for (let i = 0; i < USERS; i++) {
  const token = await ensureUser(i);
  for (let t = 0; t < TABS; t++) { sockets.push(tab(token)); await new Promise((r) => setTimeout(r, RAMP_MS)); }
  if (i % 25 === 24) console.log(`  ${sockets.length} sockets started`);
}
console.log('all tabs started. Now fire the trigger (see runbook). Stats every 10 s, deltas per window:');
let last = { ...stats };
setInterval(() => {
  const d = Object.fromEntries(Object.keys(stats).map((k) => [k, stats[k] - (last[k] || 0)]));
  last = { ...stats };
  const live = sockets.filter((s) => s.connected).length;
  console.log(new Date().toISOString().slice(11, 19), `live=${live}`, Object.entries(d).map(([k, v]) => `${k}=${v}`).join(' '));
}, 10000);
process.on('SIGINT', () => { console.log('closing sockets'); for (const s of sockets) s.close(); process.exit(0); });
