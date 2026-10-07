#!/usr/bin/env python3
"""
patch_bridge_raw.py - local raw buffer + a separate window for the UART stream.

Run on the Pi, next to the spdm_bridge/ directory, after the earlier patches:
    python3 patch_bridge_raw.py

Splits the two views apart:

  *  /        decoded SPDM traffic (as now), plus an "Open UART window" button
  *  /uart    raw byte stream in its own window - everything in and out of
              each endpoint, hex and ASCII, with direction and timestamps

Behind both sits a local ring buffer of raw chunks (8000 by default), so the
UART window can be opened at any time and still show recent history rather
than only what arrives next. The buffer is also mirrored to disk when
--raw-log is given, for keeping a record of a session.

New endpoints:
    GET /uart            the raw stream page
    GET /api/raw         raw chunks as JSON (?since=N for the tail)

Restart the framework afterwards.
"""
import sys

# ----------------------------------------------------------------- bridge.py
B = "spdm_bridge/bridge.py"
try:
    s = open(B).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "record_raw" in s:
    sys.exit("already patched")

old = """        self._control = {}
        self.last_control = {}"""
new = """        self._control = {}
        self.last_control = {}
        # local buffer of raw traffic, so the UART window can show history
        self.raw_log = []
        self.raw_limit = 8000
        self.raw_file = None
        self._raw_seq = itertools.count(1)"""
assert s.count(old) == 1, "state anchor not unique"
s = s.replace(old, new)

old = """    def _control_lines(self, name: str, data: bytes):"""
new = '''    def record_raw(self, endpoint: str, direction: str, data: bytes) -> None:
        """Buffer a chunk of raw bytes exactly as it crossed the wire."""
        if not data:
            return
        entry = {
            "seq": next(self._raw_seq),
            "t": time.time(),
            "time": time.strftime("%H:%M:%S", time.localtime()),
            "endpoint": endpoint,
            "direction": direction,          # "in" (from board) or "out" (to board)
            "n": len(data),
            "hex": " ".join(f"{b:02X}" for b in data),
            "text": "".join(chr(b) if 32 <= b < 127 else "." for b in data),
        }
        with self._lock:
            self.raw_log.append(entry)
            if len(self.raw_log) > self.raw_limit:
                del self.raw_log[: len(self.raw_log) // 4]
        if self.raw_file:
            try:
                arrow = "<-" if direction == "in" else "->"
                self.raw_file.write(
                    f"{entry['time']} {endpoint} {arrow} {entry['hex']}\\n")
                self.raw_file.flush()
            except Exception:
                pass

    def raw_snapshot(self, since: int = 0):
        with self._lock:
            return [e for e in self.raw_log if e["seq"] > since]

    def _control_lines(self, name: str, data: bytes):'''
assert s.count(old) == 1, "control anchor not unique"
s = s.replace(old, new)

# record both directions
old = """            if data:
                for line in self._control_lines(name, data):"""
new = """            if data:
                self.record_raw(name, "in", data)
                for line in self._control_lines(name, data):"""
assert s.count(old) == 1, "pump anchor not unique"
s = s.replace(old, new)

old = """        ep = self.endpoints[target]
        ep.send(data)
        self.counters["host"] += 1"""
new = """        ep = self.endpoints[target]
        self.record_raw(target, "out", data)
        ep.send(data)
        self.counters["host"] += 1"""
assert s.count(old) == 1, "send anchor not unique"
s = s.replace(old, new)

old = """        self.endpoints[target].send((text + "\\n").encode())"""
new = """        self.record_raw(target, "out", (text + "\\n").encode())
        self.endpoints[target].send((text + "\\n").encode())"""
assert s.count(old) == 1, "control send anchor not unique"
s = s.replace(old, new)

open(B, "w").write(s)
print("patched", B)

# ----------------------------------------------------------------- server.py
S = "spdm_bridge/server.py"
s = open(S).read()

UART_PAGE = '''UART_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>UART stream</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0f1115;--fg:#e6e6e6;--dim:#8b93a1;--line:#242832;
       --in:#3ddc84;--out:#4da3ff}
 @media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1a1a1a;--dim:#666;
       --line:#e2e2e2;--in:#12833b;--out:#0b66c3}}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 header{padding:10px 14px;border-bottom:1px solid var(--line);
        display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 h1{font-size:14px;margin:0;font-weight:600}
 label{color:var(--dim);font-size:12px}
 button{background:transparent;color:var(--fg);border:1px solid var(--line);
        border-radius:6px;padding:4px 9px;font:inherit;font-size:12px;cursor:pointer}
 #log{padding:8px 14px}
 .row{padding:3px 0;border-bottom:1px solid var(--line);display:flex;gap:10px}
 .t{color:var(--dim);white-space:nowrap}
 .dir{white-space:nowrap;font-weight:600}
 .in .dir{color:var(--in)} .out .dir{color:var(--out)}
 .hex{word-break:break-all;flex:1}
 .txt{color:var(--dim);white-space:pre}
 footer{position:sticky;bottom:0;background:var(--bg);padding:6px 14px;
        border-top:1px solid var(--line);color:var(--dim);font-size:12px}
</style></head><body>
<header>
  <h1>UART stream</h1>
  <label><input type="checkbox" id="follow" checked> follow</label>
  <label><input type="checkbox" id="ascii"> show ASCII</label>
  <label>endpoint
    <select id="ep"><option value="">all</option>
      <option value="fpga">fpga</option><option value="tektagon">tektagon</option>
    </select></label>
  <button onclick="document.getElementById('log').innerHTML=''">Clear</button>
</header>
<div id="log"></div>
<footer id="stat">connecting...</footer>
<script>
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
let since=0,bytes=0;
async function poll(){
  try{
    const r=await fetch('/api/raw?since='+since);
    const rows=await r.json();
    const ep=document.getElementById('ep').value;
    const showAscii=document.getElementById('ascii').checked;
    const log=document.getElementById('log');
    for(const e of rows){
      since=e.seq; bytes+=e.n;
      if(ep&&e.endpoint!==ep) continue;
      const d=document.createElement('div');
      d.className='row '+e.direction;
      d.innerHTML=`<span class="t">${esc(e.time)}</span>
        <span class="dir">${e.direction==='in'?'&lt;-':'-&gt;'} ${esc(e.endpoint)}</span>
        <span class="hex">${esc(e.hex)}</span>`+
        (showAscii?`<span class="txt">${esc(e.text)}</span>`:'');
      log.appendChild(d);
    }
    if(rows.length&&document.getElementById('follow').checked)
      window.scrollTo(0,document.body.scrollHeight);
    document.getElementById('stat').textContent=
      `${bytes} bytes buffered this session - live`;
  }catch(err){
    document.getElementById('stat').textContent='disconnected';
  }
}
poll(); setInterval(poll,400);
</script></body></html>"""


'''

old = "def make_handler(bridge: Bridge):"
assert s.count(old) == 1, "handler anchor not unique"
s = s.replace(old, UART_PAGE + old)

old = """  <button onclick="ctl('!STATUS')">Status</button>"""
new = """  <button onclick="ctl('!STATUS')">Status</button>
  <button onclick="window.open('/uart','uart','width=900,height=700')">Open UART window</button>"""
assert s.count(old) == 1, "button anchor not unique"
s = s.replace(old, new)

old = """            elif url.path == "/api/status":"""
new = """            elif url.path == "/uart":
                self._send(200, UART_PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/raw":
                since = int(parse_qs(url.query).get("since", ["0"])[0])
                self._send(200, json.dumps(bridge.raw_snapshot(since)).encode())
            elif url.path == "/api/status":"""
assert s.count(old) == 1, "route anchor not unique"
s = s.replace(old, new)

open(S, "w").write(s)
print("patched", S)

# --------------------------------------------------------------- __main__.py
M = "spdm_bridge/__main__.py"
s = open(M).read()
if "--raw-log" not in s:
    old = """    ap.add_argument("--auto", action="store_true","""
    new = """    ap.add_argument("--raw-log", help="also append the raw byte stream to this file")
    ap.add_argument("--auto", action="store_true","""
    assert s.count(old) == 1, "arg anchor not unique"
    s = s.replace(old, new)

    old = """    bridge.start()"""
    new = """    if args.raw_log:
        bridge.raw_file = open(args.raw_log, "a")
    bridge.start()"""
    assert s.count(old) == 1, "start anchor not unique"
    s = s.replace(old, new)
    open(M, "w").write(s)
    print("patched", M)

print("restart the framework to pick this up")
