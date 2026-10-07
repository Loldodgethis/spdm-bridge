#!/usr/bin/env python3
"""
patch_pi_reg_frames.py - match the frame shape the real SPDM stack expects.

Run on the Pi, next to the spdm_bridge/ directory, after
patch_reg_spdm_transport.py has been applied and built on the VCK190:
    python3 patch_pi_reg_frames.py

Why
---
Our frames carry a 4-byte MCTP transport header. spdm-lib's transport layer
wants `header_size() == 1`: one message-type byte, then the SPDM payload.
So on the register channel a frame becomes:

    7E | 05 | <SPDM: version, code, param1, param2, ...> | FCS-hi | FCS-lo | 7E

The MCTP header is not wrong in general - it is what the I3C/MCTP path uses -
it is just one layer too many for this link, where the agent and the register
channel already identify the endpoints.

What changes
------------
  * protocol.build_frame_raw()  - builds the 1-byte-header form
  * protocol.decode_frame()     - accepts both shapes, so the UART window and
                                  the decoder keep working either way
  * transports: a `raw_spdm` flag on the serial transport selects the shape
  * CLI: --frame {mctp,raw}     - default stays mctp; use raw once the VCK190
                                  is running the spdm-lib transport

Restart the framework afterwards.
"""
import sys

P = "spdm_bridge/protocol.py"
try:
    s = open(P).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "build_frame_raw" in s:
    sys.exit("already patched")

# ----------------------------------------------------- raw frame builder
old = "def decode_frame(body: bytes) -> Message:"
new = '''def build_frame_raw(spdm_code: int, payload: bytes = b"\\x00\\x00", *,
                    spdm_version: int = 0x10,
                    msg_type: int = MSG_TYPE_SPDM) -> bytes:
    """Frame for a transport with header_size() == 1 (spdm-lib's view).

    Layout inside the flags: one message-type byte, the SPDM message, then
    the FCS. No MCTP transport header - the register channel identifies the
    endpoints by itself.
    """
    body = bytes([msg_type & 0x7F, spdm_version, spdm_code]) + payload
    fcs = fcs16(body)
    framed = body + bytes([(fcs >> 8) & 0xFF, fcs & 0xFF])
    return bytes([FLAG]) + escape(framed) + bytes([FLAG])


def canned_request_stream_raw() -> bytes:
    return b"".join(build_frame_raw(c, p) for c, p in REQUEST_SEQUENCE)


def _decode_raw(body: bytes) -> Message:
    """Decode a 1-byte-header frame: type, SPDM payload, FCS."""
    raw = body
    want = (body[-2] << 8) | body[-1]
    if fcs16(body[:-2]) != want:
        return Message(0, 0, False, False, 0, False, 0, raw=raw,
                       error=f"FCS {fcs16(body[:-2]):#06x} != {want:#06x}")
    inner = body[:-2]
    msg = Message(dest_eid=0, src_eid=0, som=True, eom=True, seq=0,
                  tag_owner=False, msg_tag=0, raw=raw)
    msg.msg_type = inner[0] & 0x7F
    if len(inner) >= 3:
        msg.spdm_version = inner[1]
        msg.spdm_code = inner[2]
        msg.payload = inner[3:]
    return msg


def decode_frame(body: bytes) -> Message:'''
assert s.count(old) == 1, "decode anchor not unique"
s = s.replace(old, new, 1)

# ------------------------------------------- accept both shapes on decode
old = """    rev, length = body[0], body[1]
    want = (body[-2] << 8) | body[-1]
    got = fcs16(body[:-2])
    if rev != REV:"""
new = """    # A frame whose first byte is an SPDM message type is the 1-byte-header
    # form used on the register channel; anything else is read as DSP0253.
    if body[0] & 0x7F in MSG_TYPES and body[0] != REV:
        return _decode_raw(body)

    rev, length = body[0], body[1]
    want = (body[-2] << 8) | body[-1]
    got = fcs16(body[:-2])
    if rev != REV:"""
assert s.count(old) == 1, "shape anchor not unique"
s = s.replace(old, new, 1)

open(P, "w").write(s)
print("patched", P)

# ------------------------------------------------------------- server.py
S = "spdm_bridge/server.py"
s = open(S).read()
if "canned_request_stream_raw" not in s:
    s = s.replace("protocol.canned_request_stream()",
                  "(protocol.canned_request_stream_raw() if getattr(bridge, 'raw_frames', False)"
                  " else protocol.canned_request_stream())")
    s = s.replace("""                    payload = bytes.fromhex(body.get("payload", "0000"))
                    data = protocol.build_frame(int(body["code"]), payload)""",
"""                    payload = bytes.fromhex(body.get("payload", "0000"))
                    builder = (protocol.build_frame_raw
                               if getattr(bridge, "raw_frames", False)
                               else protocol.build_frame)
                    data = builder(int(body["code"]), payload)""")
    open(S, "w").write(s)
    print("patched", S)

# ------------------------------------------------------------ __main__.py
M = "spdm_bridge/__main__.py"
s = open(M).read()
if "--frame" not in s:
    old = """    ap.add_argument("--mode","""
    new = """    ap.add_argument("--frame", choices=["mctp", "raw"], default="mctp",
                    help="frame shape on the FPGA link: 'mctp' keeps the 4-byte "
                         "MCTP header, 'raw' sends the 1-byte message-type "
                         "header spdm-lib expects")
    ap.add_argument("--mode","""
    assert s.count(old) == 1, "arg anchor not unique"
    s = s.replace(old, new, 1)

    old = """    bridge.start()"""
    new = """    bridge.raw_frames = args.frame == "raw"
    bridge.start()"""
    assert s.count(old) == 1, "start anchor not unique"
    s = s.replace(old, new, 1)
    open(M, "w").write(s)
    print("patched", M)

print("restart the framework; use --frame raw once the VCK190 runs the "
      "spdm-lib register transport")
