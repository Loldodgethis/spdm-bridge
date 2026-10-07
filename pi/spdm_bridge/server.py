"""
server.py - web GUI for the bridge. Standard library only (no Flask needed).

Serves a single page that streams the message log live over server-sent events,
showing both sides of the conversation side by side. Runs on the Pi; open it
from a laptop browser at http://<pi>:8080.

Endpoints:
    GET  /              the page
    GET  /events        SSE stream of log entries
    GET  /api/status    endpoint status + counters
    GET  /api/log       whole log as JSON (?since=N for the tail)
    POST /api/send      {"target": "fpga", "code": 132} or {"hex": "7E 01 ..."}
    POST /api/sequence  run the canned GET_VERSION..CHALLENGE exchange
"""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import protocol
from .bridge import Bridge

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>SPDM Bridge</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
 :root{--bg:#0f1115;--fg:#e6e6e6;--dim:#8b93a1;--line:#242832;
       --req:#4da3ff;--resp:#3ddc84;--err:#ff6b6b;--other:#c792ea}
 @media(prefers-color-scheme:light){:root{--bg:#fff;--fg:#1a1a1a;--dim:#666;
       --line:#e2e2e2;--req:#0b66c3;--resp:#12833b;--err:#c62828;--other:#7b2fbf}}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
 header{padding:12px 16px;border-bottom:1px solid var(--line);
        display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 h1{font-size:15px;margin:0;font-weight:600}
 .pill{border:1px solid var(--line);border-radius:999px;padding:2px 10px;
       font-size:12px;color:var(--dim)}
 button{background:transparent;color:var(--fg);border:1px solid var(--line);
        border-radius:6px;padding:5px 10px;font:inherit;font-size:12px;cursor:pointer}
 button:hover{border-color:var(--dim)}
 main{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--line);
      height:calc(100vh - 106px)}
 @media(max-width:800px){main{grid-template-columns:1fr;height:auto}}
 section{background:var(--bg);overflow-y:auto;padding:8px}
 h2{font-size:12px;color:var(--dim);margin:4px 8px 8px;font-weight:600;
    text-transform:uppercase;letter-spacing:.06em}
 .row{padding:6px 8px;border-bottom:1px solid var(--line)}
 .row:hover{background:rgba(127,127,127,.07)}
 .top{display:flex;gap:8px;align-items:baseline}
 .t{color:var(--dim);font-size:11px}
 .name{font-weight:600}
 .request .name{color:var(--req)} .response .name{color:var(--resp)}
 .error .name{color:var(--err)} .other .name{color:var(--other)}
 .sum{color:var(--dim);font-size:12px}
 .hex{color:var(--dim);font-size:11px;word-break:break-all;margin-top:2px}
 footer{padding:8px 16px;border-top:1px solid var(--line);color:var(--dim);
        font-size:12px;display:flex;gap:16px;flex-wrap:wrap}
</style></head><body>
<header>
  <h1>SPDM Bridge</h1>
  <span class="pill" id="st-fpga">fpga: ...</span>
  <span class="pill" id="st-tek">tektagon: ...</span>
  <button onclick="post('/api/attest',{target:'fpga'})">Run attestation</button>
  <button onclick="run()">Boot + Run</button>
  <button onclick="seq()">Run SPDM sequence</button>
  <button onclick="ctl('!BOOT')">Boot firmware</button>
  <button onclick="ctl('!STOP')">Stop firmware</button>
  <button onclick="ctl('!STATUS')">Status</button>
  <button onclick="window.open('/uart','uart','width=900,height=700')">Open UART window</button>
  <button onclick="one(0x84)">GET_VERSION</button>
  <button onclick="one(0xE1)">GET_CAPABILITIES</button>
  <button onclick="one(0x81)">GET_DIGESTS</button>
  <button onclick="one(0x83)">CHALLENGE</button>
  <button onclick="document.querySelectorAll('.list').forEach(e=>e.innerHTML='')">Clear</button>
</header>
<main>
  <section><h2>FPGA / Caliptra MCU</h2><div class="list" id="fpga"></div></section>
  <section><h2>Tektagon</h2><div class="list" id="tektagon"></div></section>
</main>
<footer>
  <span id="counts"></span><span id="conn">connecting...</span>
</footer>
<script>
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function add(e){
  const col=(e.source==='tektagon'||e.target==='tektagon')?'tektagon':'fpga';
  const box=document.getElementById(col); if(!box) return;
  const d=document.createElement('div');
  d.className='row '+e.kind;
  d.innerHTML=`<div class="top"><span class="t">${esc(e.time)}</span>
    <span class="name">${esc(e.name)}</span>
    <span class="t">${esc(e.direction)}</span></div>
    <div class="sum">${esc(e.summary)}</div>
    ${e.hexdump?`<div class="hex">${esc(e.hexdump)}</div>`:''}`;
  box.appendChild(d);
  box.parentElement.scrollTop=box.parentElement.scrollHeight;
}
function status(s){
  const f=s.endpoints.fpga,t=s.endpoints.tektagon;
  document.getElementById('st-fpga').textContent='fpga: '+f.status;
  document.getElementById('st-tek').textContent='tektagon: '+t.status;
  const c=s.counters;
  document.getElementById('counts').textContent=
    `fpga ${c.fpga} | tektagon ${c.tektagon} | sent ${c.host} | errors ${c.errors}`;
}
async function post(url,body){await fetch(url,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});}
const seq=()=>post('/api/sequence',{target:'fpga'});
const ctl=t=>post('/api/control',{target:'fpga',text:t});
const run=()=>post('/api/run',{target:'fpga'});
const one=code=>post('/api/send',{target:'fpga',code:code});
const ev=new EventSource('/events');
ev.onopen=()=>document.getElementById('conn').textContent='live';
ev.onerror=()=>document.getElementById('conn').textContent='disconnected';
ev.onmessage=m=>{const d=JSON.parse(m.data); d.status?status(d.status):add(d);};
fetch('/api/status').then(r=>r.json()).then(status);
fetch('/api/log').then(r=>r.json()).then(l=>l.forEach(add));
setInterval(()=>fetch('/api/status').then(r=>r.json()).then(status),3000);
</script></body></html>"""


UART_PAGE = """<!doctype html>
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


def make_handler(bridge: Bridge):
    subscribers: "list[queue.Queue]" = []
    lock = threading.Lock()

    def fanout(entry):
        with lock:
            subs = list(subscribers)
        for q in subs:
            q.put(entry.as_dict())

    bridge.subscribe(fanout)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):  # quiet
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/uart":
                self._send(200, UART_PAGE.encode(), "text/html; charset=utf-8")
            elif url.path == "/api/raw":
                since = int(parse_qs(url.query).get("since", ["0"])[0])
                self._send(200, json.dumps(bridge.raw_snapshot(since)).encode())
            elif url.path == "/api/attest/state":
                self._send(200, json.dumps(
                    getattr(bridge, "attest_state", {})).encode())
            elif url.path == "/api/status":
                self._send(200, json.dumps(bridge.status()).encode())
            elif url.path == "/api/log":
                since = int(parse_qs(url.query).get("since", ["0"])[0])
                self._send(200, json.dumps(bridge.snapshot(since)).encode())
            elif url.path == "/events":
                self._events()
            else:
                self._send(404, b'{"error":"not found"}')

        def _events(self):
            q: "queue.Queue" = queue.Queue()
            with lock:
                subscribers.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    try:
                        item = q.get(timeout=15)
                        payload = json.dumps(item)
                    except queue.Empty:
                        payload = json.dumps({"status": bridge.status()})
                    self.wfile.write(f"data: {payload}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with lock:
                    if q in subscribers:
                        subscribers.remove(q)

        def do_POST(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, b'{"error":"bad json"}')
            target = body.get("target", "fpga")
            if target not in bridge.endpoints:
                return self._send(400, b'{"error":"unknown target"}')

            if url.path == "/api/send":
                if "hex" in body:
                    data = bytes.fromhex(body["hex"].replace(" ", ""))
                elif "code" in body:
                    payload = bytes.fromhex(body.get("payload", "0000"))
                    ver = body.get("version", bridge.negotiated_version)
                    if getattr(bridge, "raw_frames", False):
                        data = protocol.build_frame_raw(
                            int(body["code"]), payload, spdm_version=ver)
                    else:
                        data = protocol.build_frame(
                            int(body["code"]), payload, spdm_version=ver)
                else:
                    return self._send(400, b'{"error":"need code or hex"}')
                threading.Thread(target=bridge.send, args=(target, data),
                                 daemon=True).start()
                self._send(200, b'{"ok":true}')
            elif url.path == "/api/control":
                text = body.get("text", "!STATUS")
                threading.Thread(target=bridge.control, args=(target, text),
                                 daemon=True).start()
                self._send(200, b'{"ok":true}')
            elif url.path == "/api/run":
                def boot_and_run():
                    import time as _t

                    # Firmware only lives while a test runs. Ask the agent
                    # what state it is in, then do the least work needed.
                    bridge.control(target, "!STATUS")
                    _t.sleep(1.5)
                    state = bridge.last_control.get(target)

                    if state != "!UP":
                        bridge.control(target, "!BOOT")
                        _t.sleep(1.0)
                        if bridge.last_control.get(target) == "!BUSY":
                            # a previous run is still going; let it finish
                            bridge.note("a test is already running - waiting",
                                        kind="info")
                            bridge.wait_for(target, "!DOWN", 90)
                            bridge.control(target, "!BOOT")
                        if not bridge.wait_for(target, "!READY", 150):
                            bridge.note("firmware did not come up in time",
                                        kind="error")
                            return

                    bridge.send(target, (protocol.canned_request_stream_raw() if getattr(bridge, 'raw_frames', False) else protocol.canned_request_stream()))

                threading.Thread(target=boot_and_run, daemon=True).start()
                self._send(200, b'{"ok":true}')
            elif url.path == "/api/attest":
                def attest():
                    import time as _t

                    state = {}
                    build = (protocol.build_frame_raw
                             if getattr(bridge, "raw_frames", False)
                             else protocol.build_frame)
                    ver = 0x10

                    for code, body_fn, want, name in protocol.ATTESTATION_FLOW:
                        body = body_fn()
                        bridge.send(target, build(code, body, spdm_version=ver))
                        rsp = bridge.wait_response(want, timeout=25)
                        if rsp is None:
                            bridge.note(f"{name}: no response - stopping",
                                        kind="error")
                            break

                        if want == 0x04:        # VERSION: pick what it offers
                            offered = protocol.parse_version_response(rsp)
                            if offered:
                                ver = offered[0]
                                state["versions"] = [f"1.{v & 0xF}" for v in offered]
                                bridge.note(
                                    "offers SPDM "
                                    + ", ".join(state["versions"])
                                    + f" - continuing at 1.{ver & 0xF}")
                        elif want == 0x61:      # CAPABILITIES
                            info = protocol.parse_capabilities(rsp)
                            state["capabilities"] = info
                            if info:
                                bridge.note(
                                    "capabilities: "
                                    + ", ".join(info["flag_names"])
                                    + f" | DataTransferSize "
                                    f"{info.get('data_transfer_size', '?')}")
                        elif want == 0x02:      # CERTIFICATE
                            state["certificate_bytes"] = len(rsp.payload)
                            bridge.note(
                                f"certificate chunk: {len(rsp.payload)} bytes")
                        elif want == 0x03:      # CHALLENGE_AUTH
                            state["challenge_auth_bytes"] = len(rsp.payload)
                            bridge.note(
                                f"challenge auth: {len(rsp.payload)} bytes "
                                "(includes signature)")
                        elif want == 0x60:      # MEASUREMENTS
                            state["measurement_bytes"] = len(rsp.payload)
                            bridge.note(
                                f"measurements: {len(rsp.payload)} bytes signed")

                        _t.sleep(0.3)

                    bridge.attest_state = state
                    done = len(state)
                    bridge.note(f"attestation run finished, {done} stage(s) "
                                "returned data")

                threading.Thread(target=attest, daemon=True).start()
                self._send(200, b'{"ok":true}')
            elif url.path == "/api/sequence":
                def negotiated_sequence():
                    import time as _t

                    raw = getattr(bridge, "raw_frames", False)
                    build = protocol.build_frame_raw if raw else protocol.build_frame

                    # 1. GET_VERSION always goes out at 1.0: every responder
                    #    must accept it, and its answer tells us what to use.
                    bridge.send(target, build(0x84, b"\x00\x00",
                                              spdm_version=0x10))
                    deadline = _t.monotonic() + 10
                    start = bridge.negotiated_version
                    while _t.monotonic() < deadline:
                        if bridge.negotiated_version != start:
                            break
                        _t.sleep(0.1)

                    # 2. the rest at whatever it offered
                    ver = bridge.negotiated_version
                    for code, payload in protocol.REQUEST_SEQUENCE[1:]:
                        bridge.send(target, build(code, payload, spdm_version=ver))
                        _t.sleep(0.5)

                threading.Thread(target=negotiated_sequence, daemon=True).start()
                self._send(200, b'{"ok":true}')
                return
            elif url.path == "/api/sequence-raw":
                threading.Thread(
                    target=bridge.send,
                    args=(target, (protocol.canned_request_stream_raw() if getattr(bridge, 'raw_frames', False) else protocol.canned_request_stream())),
                    daemon=True).start()
                self._send(200, b'{"ok":true}')
            else:
                self._send(404, b'{"error":"not found"}')

    return Handler


def serve(bridge: Bridge, host: str = "0.0.0.0", port: int = 8080):
    server = ThreadingHTTPServer((host, port), make_handler(bridge))
    server.daemon_threads = True
    return server
