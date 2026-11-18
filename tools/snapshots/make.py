"""Render annotated code snapshots from pristine open-webui v0.12.5 sources."""
import html
import sys
from pathlib import Path

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import PythonLexer, TypeScriptLexer

SRC = Path('/home/claude/snap/src')
OUT = Path('/home/claude/snap/out')
OUT.mkdir(exist_ok=True)
TAG = 'v0.12.5'
REPO = 'https://github.com/open-webui/open-webui/blob'

FILES = {
    'backend/open_webui/socket/main.py': 'backend_open_webui_socket_main.py',
    'backend/open_webui/utils/auth.py': 'backend_open_webui_utils_auth.py',
    'backend/open_webui/utils/mfa.py': 'backend_open_webui_utils_mfa.py',
    'src/routes/+layout.svelte': 'src_routes_+layout.svelte',
    'src/routes/(app)/+layout.svelte': 'src_routes_app_+layout.svelte',
    'src/lib/components/layout/Sidebar.svelte': 'src_lib_components_layout_Sidebar.svelte',
    'src/lib/components/chat/Chat.svelte': 'src_lib_components_chat_Chat.svelte',
}

_cache = {}


def lines_of(path):
    """Highlighted HTML per source line (1-based list), highlighting the whole file for correct state."""
    if path in _cache:
        return _cache[path]
    text = (SRC / FILES[path]).read_text()
    raw = text.split('\n')
    if path.endswith('.py'):
        out = highlight(text, PythonLexer(stripnl=False), HtmlFormatter(nowrap=True)).split('\n')
    else:
        # Svelte: highlight each line range later with the TS lexer; keep raw here.
        out = None
    _cache[path] = (raw, out)
    return _cache[path]


def render_range(path, a, b):
    raw, hl = lines_of(path)
    if hl is not None:
        return {n: hl[n - 1] for n in range(a, b + 1)}
    chunk = '\n'.join(raw[a - 1:b])
    h = highlight(chunk, TypeScriptLexer(stripnl=False), HtmlFormatter(nowrap=True)).split('\n')
    return {a + i: h[i] if i < len(h) else '' for i in range(b - a + 1)}


def check(path, a, b, rendered):
    import re
    raw = lines_of(path)[0]
    for n, h in rendered.items():
        txt = html.unescape(re.sub(r'<[^>]+>', '', h))
        assert txt.rstrip() == raw[n - 1].rstrip(), (path, n, txt, raw[n - 1])


def panel_html(p):
    path = p['file']
    marks = p.get('marks', {})
    hot = set(p.get('hot', [])) | set(marks)
    rows = []
    prev = None
    for a, b in p['ranges']:
        if prev is not None and a > prev + 1:
            rows.append(f'<tr class="gap"><td class="ln"></td><td class="mk"></td>'
                        f'<td class="src">⋯ {a - prev - 1} שורות ⋯</td></tr>')
        rr = render_range(path, a, b)
        check(path, a, b, rr)
        for n, code in rr.items():
            cls = ' class="hot"' if n in hot else ''
            mk = f'<span class="mark">{marks[n]}</span>' if n in marks else ''
            rows.append(f'<tr{cls}><td class="ln">{n}</td><td class="mk">{mk}</td>'
                        f'<td class="src">{code or " "}</td></tr>')
        prev = b
    first, last = p['ranges'][0][0], p['ranges'][-1][1]
    return (f'<div class="panel"><div class="ph"><span class="path">{html.escape(path)}</span>'
            f'<span class="ver">{TAG} · L{first}–L{last}</span></div>'
            f'<table class="code">{"".join(rows)}</table></div>')


def bidi(t):
    import re
    return re.sub(r'[A-Za-z_/+][A-Za-z0-9_./:+\-()]*[A-Za-z0-9_)]|[A-Za-z]', lambda m: f'<bdi dir="ltr">{m.group(0)}</bdi>', t)


def page(s, idx, total):
    panels = ''.join(panel_html(p) for p in s['panels'])
    notes = ''.join(f'<div class="note"><span class="mark">{n}</span><div>{t}</div></div>' for n, t in s['notes'])
    extra = f'<div class="callout">{s["callout"]}</div>' if s.get('callout') else ''
    links = ''.join(
        f'<div>{REPO}/{TAG}/{p["file"]}#L{p["ranges"][0][0]}-L{p["ranges"][-1][1]}</div>' for p in s['panels'])
    css = HtmlFormatter(style='github-dark').get_style_defs('.src')
    return f'''<!DOCTYPE html><html><head><meta charset="utf-8"><style>
{css}
body{{margin:0;background:#010409}}
.wrap{{display:inline-block;min-width:1240px;max-width:1500px;padding:28px 34px 22px;background:#0d1117;
  font-family:"Liberation Sans","DejaVu Sans",sans-serif;color:#e6edf3}}
.head{{direction:rtl;display:flex;gap:16px;align-items:center}}
.badge{{flex:none;background:#f85149;color:#fff;font-weight:700;font-size:20px;border-radius:10px;padding:6px 12px;
  direction:ltr}}
.title{{font-weight:700;font-size:27px;line-height:1.3}}
.sub{{direction:rtl;color:#9da7b3;font-size:18px;margin-top:8px}}
.panel{{border:1px solid #30363d;border-radius:10px;margin-top:18px;overflow:hidden}}
.ph{{background:#161b22;border-bottom:1px solid #30363d;padding:9px 14px;display:flex;justify-content:space-between;
  font:15px "DejaVu Sans Mono",monospace;color:#c9d1d9;direction:ltr}}
.ph .ver{{color:#7d8590}}
table.code{{border-collapse:collapse;width:100%;font:15px/1.55 "DejaVu Sans Mono",monospace;direction:ltr}}
td.ln{{width:52px;text-align:right;color:#6e7681;padding:0 10px 0 8px;border-left:3px solid transparent}}
td.mk{{width:28px;text-align:center}}
td.src{{white-space:pre;tab-size:4;color:#e6edf3;padding-right:16px}}
tr.hot td{{background:rgba(248,81,73,.16)}}
tr.hot td.ln{{color:#ffa198;border-left-color:#f85149}}
tr.gap td{{background:#161b22;color:#7d8590;font-size:13px;text-align:left;padding:2px 10px;
  font-family:"Liberation Sans",sans-serif;direction:rtl}}
.mark{{display:inline-block;min-width:22px;height:22px;border-radius:11px;background:#f85149;color:#fff;
  font:700 13px/22px "Liberation Sans",sans-serif;text-align:center;vertical-align:middle}}
.notes{{direction:rtl;margin-top:18px;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:12px 18px;
  font-size:19px;line-height:1.55}}
.note{{display:flex;gap:12px;align-items:flex-start;margin:7px 0}}
.note .mark{{flex:none;margin-top:3px}}
code{{direction:ltr;unicode-bidi:isolate;font:15px "DejaVu Sans Mono",monospace;background:#0d1117;
  border:1px solid #30363d;padding:0 5px;border-radius:5px;color:#ffa657}}
b{{color:#fff}}
.callout{{direction:rtl;margin-top:14px;border-right:4px solid #d29922;background:rgba(210,153,34,.12);
  padding:10px 16px;border-radius:6px;font-size:19px;line-height:1.55}}
.foot{{direction:ltr;color:#6e7681;font:12.5px "DejaVu Sans Mono",monospace;margin-top:14px;line-height:1.6}}
</style></head><body><div class="wrap">
<div class="head"><span class="badge">{idx} / {total}</span><div class="title">{bidi(s["title"])}</div></div>
<div class="sub">{bidi(s["sub"])}</div>
{panels}
<div class="notes">{notes}</div>
{extra}
<div class="foot">{links}</div>
</div></body></html>'''


def main():
    from snapshots import SNAPS
    from playwright.sync_api import sync_playwright
    exe = '/opt/pw-browsers/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell'
    only = set(sys.argv[1:])
    with sync_playwright() as pw:
        br = pw.chromium.launch(executable_path=exe, args=['--no-sandbox'])
        pg = br.new_page(viewport={'width': 1600, 'height': 900}, device_scale_factor=2)
        for i, s in enumerate(SNAPS, 1):
            if only and str(i) not in only:
                continue
            name = f'{i:02d}-{s["slug"]}'
            h = OUT / f'{name}.html'
            h.write_text(page(s, i, len(SNAPS)))
            pg.goto(h.as_uri())
            pg.wait_for_timeout(150)
            pg.locator('.wrap').screenshot(path=str(OUT / f'{name}.png'))
            print('ok', name)
        br.close()


if __name__ == '__main__':
    main()
