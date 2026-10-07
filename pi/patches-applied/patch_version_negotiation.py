#!/usr/bin/env python3
"""
patch_version_negotiation.py - negotiate the SPDM version instead of assuming 1.0.

Run on the Pi, next to the spdm_bridge/ directory:
    python3 patch_version_negotiation.py

Why
---
The real Caliptra responder answers GET_VERSION with a version-entry table -
observed on hardware as:

    05 10 04 00 00 02 00 13 00 12      -> 2 entries: SPDM 1.3 and 1.2

It does NOT offer 1.0, so every follow-up request sent at 1.0 comes back as
ERROR (0x7F). SPDM requires the requester to pick a version the responder
listed and use it for the rest of the conversation.

What this adds
--------------
  * protocol.parse_version_response() - pull the offered versions out of a
    VERSION message
  * bridge.negotiated_version - set when a VERSION response is seen, and used
    for every request built afterwards
  * the canned sequence sends GET_VERSION first, waits for the answer, then
    sends the rest at the negotiated version

Restart the framework afterwards.
"""
import sys

# ----------------------------------------------------------------- protocol
P = "spdm_bridge/protocol.py"
try:
    s = open(P).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "parse_version_response" in s:
    sys.exit("already patched")

old = "def build_frame_raw("
new = '''def parse_version_response(msg: "Message") -> List[int]:
    """Versions a VERSION response offers, highest first.

    After the SPDM header the payload holds param1, param2, then a version
    count and that many 16-bit entries whose high byte is the version as
    0xMm. Implementations differ on whether a reserved byte sits before the
    count, so the count is located by checking which position leaves exactly
    the right number of entry bytes.

    Observed on Caliptra hardware:
        05 10 04 | 00 00 02 00 13 00 12   -> count 2, versions 1.3 and 1.2
    """
    if msg.spdm_code != 0x04:
        return []
    payload = msg.payload
    for at in (2, 3):
        if at >= len(payload):
            continue
        count = payload[at]
        if count and len(payload) == at + 1 + 2 * count:
            return sorted(
                (payload[at + 1 + i * 2 + 1] for i in range(count)),
                reverse=True)
    return []


def build_frame_raw('''
assert s.count(old) == 1, "builder anchor not unique"
s = s.replace(old, new, 1)

open(P, "w").write(s)
print("patched", P)

# ------------------------------------------------------------------- bridge
B = "spdm_bridge/bridge.py"
s = open(B).read()
if "negotiated_version" not in s:
    old = """        self.raw_log = []"""
    new = """        # SPDM version agreed with the responder; set from its VERSION
        # response. 0x10 only until we have been told otherwise.
        self.negotiated_version = 0x10
        self.raw_log = []"""
    assert s.count(old) == 1, "state anchor not unique"
    s = s.replace(old, new, 1)

    old = """        self.counters[source] = self.counters.get(source, 0) + 1"""
    new = """        # Learn the version from a VERSION response so later requests match.
        if not msg.error and msg.spdm_code == 0x04:
            offered = protocol.parse_version_response(msg)
            if offered:
                self.negotiated_version = offered[0]
                self.note(
                    "responder offers SPDM "
                    + ", ".join(f"1.{v & 0xF}" for v in offered)
                    + f" - using 1.{offered[0] & 0xF}",
                    kind="info")

        self.counters[source] = self.counters.get(source, 0) + 1"""
    assert s.count(old) == 1, "handle anchor not unique"
    s = s.replace(old, new, 1)
    open(B, "w").write(s)
    print("patched", B)

# ------------------------------------------------------------------- server
S = "spdm_bridge/server.py"
s = open(S).read()
if "negotiated_version" not in s:
    # single request: build at the negotiated version
    old = """                    builder = (protocol.build_frame_raw
                               if getattr(bridge, "raw_frames", False)
                               else protocol.build_frame)
                    data = builder(int(body["code"]), payload)"""
    new = """                    ver = body.get("version", bridge.negotiated_version)
                    if getattr(bridge, "raw_frames", False):
                        data = protocol.build_frame_raw(
                            int(body["code"]), payload, spdm_version=ver)
                    else:
                        data = protocol.build_frame(
                            int(body["code"]), payload, spdm_version=ver)"""
    assert s.count(old) == 1, "single-send anchor not unique"
    s = s.replace(old, new, 1)

    # the canned sequence: GET_VERSION, wait, then the rest at the agreed version
    old = """            elif url.path == "/api/sequence":"""
    new = '''            elif url.path == "/api/sequence":
                def negotiated_sequence():
                    import time as _t

                    raw = getattr(bridge, "raw_frames", False)
                    build = protocol.build_frame_raw if raw else protocol.build_frame

                    # 1. GET_VERSION always goes out at 1.0: every responder
                    #    must accept it, and its answer tells us what to use.
                    bridge.send(target, build(0x84, b"\\x00\\x00",
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
                self._send(200, b\'{"ok":true}\')
                return
            elif url.path == "/api/sequence-raw":'''
    assert s.count(old) == 1, "sequence anchor not unique"
    s = s.replace(old, new, 1)
    open(S, "w").write(s)
    print("patched", S)

print("restart the framework to pick this up")
