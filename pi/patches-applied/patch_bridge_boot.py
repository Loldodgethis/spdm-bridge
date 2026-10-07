#!/usr/bin/env python3
"""
patch_bridge_boot.py - add firmware control and one-click run to the framework.

Run on the Pi, in the directory holding the spdm_bridge package:
    python3 patch_bridge_boot.py

Adds:
  * control-line handling - the agent's !READY / !DOWN / !BOOTING replies are
    logged as notes instead of being swallowed as frame noise
  * POST /api/control {"text": "!BOOT"} - send a control line to the agent
  * POST /api/run - boot firmware, wait for !READY, then send the SPDM sequence
  * "Boot + Run" button in the GUI

With this, the Pi drives everything over USB: no laptop, no network, no
manual steps on the VCK190 (its agent runs as a service).
"""
import sys

# ----------------------------------------------------------------- bridge.py
B = "spdm_bridge/bridge.py"
try:
    s = open(B).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "control_line" in s:
    sys.exit("already patched")

old = """            if data:
                for msg in decoder.feed(data):
                    self._handle(name, msg)"""
new = """            if data:
                for line in self._control_lines(name, data):
                    self.note(f"{name}: {line}", kind="info")
                for msg in decoder.feed(data):
                    self._handle(name, msg)"""
assert s.count(old) == 1, "pump anchor not unique"
s = s.replace(old, new)

old = """    def send(self, target: str, data: bytes, label: str = "host") -> None:"""
new = '''    def _control_lines(self, name: str, data: bytes):
        """Pull '!...' status lines (from the agent) out of the byte stream.

        They travel outside SPDM frames, so the frame decoder ignores them;
        this surfaces them in the log instead of dropping them silently.
        """
        buf = self._control.setdefault(name, bytearray())
        lines = []
        for b in data:
            if b in (0x0A, 0x0D):
                text = buf.decode("ascii", "ignore").strip()
                buf.clear()
                if text.startswith("!"):
                    lines.append(text)
                    self.last_control[name] = text
            elif b == 0x7E:
                buf.clear()
            elif 32 <= b < 127:
                if len(buf) < 64:
                    buf.append(b)
        return lines

    def control(self, target: str, text: str) -> None:
        """Send a control line (e.g. !BOOT) to one endpoint's agent."""
        self.endpoints[target].send((text + "\\n").encode())
        self.note(f"-> {target}: {text}", kind="info")

    def wait_for(self, target: str, token: str, timeout: float = 120.0) -> bool:
        """Block until the agent reports `token`, or give up."""
        import time as _t

        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            if self.last_control.get(target) == token:
                return True
            _t.sleep(0.2)
        return False

    def send(self, target: str, data: bytes, label: str = "host") -> None:'''
assert s.count(old) == 1, "send anchor not unique"
s = s.replace(old, new)

old = """        self._decoders = {k: protocol.FrameDecoder() for k in self.endpoints}"""
new = """        self._decoders = {k: protocol.FrameDecoder() for k in self.endpoints}
        self._control = {}
        self.last_control = {}"""
assert s.count(old) == 1, "state anchor not unique"
s = s.replace(old, new)

open(B, "w").write(s)
print("patched", B)

# ----------------------------------------------------------------- server.py
S = "spdm_bridge/server.py"
s = open(S).read()

old = """  <button onclick="seq()">Run SPDM sequence</button>"""
new = """  <button onclick="run()">Boot + Run</button>
  <button onclick="seq()">Run SPDM sequence</button>
  <button onclick="ctl('!BOOT')">Boot firmware</button>
  <button onclick="ctl('!STATUS')">Status</button>"""
assert s.count(old) == 1, "button anchor not unique"
s = s.replace(old, new)

old = """const seq=()=>post('/api/sequence',{target:'fpga'});"""
new = """const seq=()=>post('/api/sequence',{target:'fpga'});
const ctl=t=>post('/api/control',{target:'fpga',text:t});
const run=()=>post('/api/run',{target:'fpga'});"""
assert s.count(old) == 1, "js anchor not unique"
s = s.replace(old, new)

old = """            elif url.path == "/api/sequence":"""
new = '''            elif url.path == "/api/control":
                text = body.get("text", "!STATUS")
                threading.Thread(target=bridge.control, args=(target, text),
                                 daemon=True).start()
                self._send(200, b'{"ok":true}')
            elif url.path == "/api/run":
                def boot_and_run():
                    # firmware only lives while a test runs, so ask for one,
                    # wait until the MCU answers, then send the requests
                    if bridge.last_control.get(target) != "!READY":
                        bridge.control(target, "!BOOT")
                        if not bridge.wait_for(target, "!READY", 150):
                            bridge.note("firmware did not come up in time",
                                        kind="error")
                            return
                    bridge.send(target, protocol.canned_request_stream())

                threading.Thread(target=boot_and_run, daemon=True).start()
                self._send(200, b'{"ok":true}')
            elif url.path == "/api/sequence":'''
assert s.count(old) == 1, "route anchor not unique"
s = s.replace(old, new)

open(S, "w").write(s)
print("patched", S)
print("restart the framework to pick this up")
