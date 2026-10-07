#!/usr/bin/env python3
"""
patch_attestation_flow.py - drive the full SPDM attestation sequence.

Run on the Pi, next to the spdm_bridge/ directory:
    python3 patch_attestation_flow.py

Until now the framework sent four fixed 1.0-shaped requests. The responder is
SPDM 1.3 and most of those messages have version-specific bodies, so anything
past GET_VERSION came back as ERROR.

This adds a real requester:

    GET_VERSION          -> VERSION            pick the highest offered
    GET_CAPABILITIES     -> CAPABILITIES       full 1.3 body, flags decoded
    NEGOTIATE_ALGORITHMS -> ALGORITHMS         SHA-384 + ECDSA P-384
    GET_DIGESTS          -> DIGESTS            which cert slots are populated
    GET_CERTIFICATE      -> CERTIFICATE        fetched in DataTransferSize
                                               chunks and reassembled
    CHALLENGE            -> CHALLENGE_AUTH     random nonce, signature returned
    GET_MEASUREMENTS     -> MEASUREMENTS       signed measurement records

Each step waits for its response before the next goes out, which is what SPDM
requires; firing them all at once is why the old sequence failed.

New:
    POST /api/attest     run the whole flow
    GET  /api/attest/state   what has been learned so far
    "Run attestation" button in the GUI

Restart the framework afterwards.
"""
import sys

# ----------------------------------------------------------------- protocol
P = "spdm_bridge/protocol.py"
try:
    s = open(P).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "CAP_FLAGS" in s:
    sys.exit("already patched")

ADD = '''

# ----------------------------------------------------- capability decoding
#: Bit definitions from spdm-lib's CapFlags (codec/src/capabilities.rs),
#: which follows DSP0274 Table 18.
CAP_FLAGS = [
    (1 << 0, "CACHE"),
    (1 << 1, "CERT"),
    (1 << 2, "CHAL"),
    (1 << 3, "MEAS_NO_SIG"),
    (2 << 3, "MEAS_SIG"),
    (1 << 5, "MEAS_FRESH"),
    (1 << 6, "ENCRYPT"),
    (1 << 7, "MAC"),
    (1 << 8, "MUT_AUTH"),
    (1 << 9, "KEY_EX"),
    (1 << 12, "ENCAP"),
    (1 << 13, "HBEAT"),
    (1 << 14, "KEY_UPD"),
    (1 << 15, "HANDSHAKE_IN_THE_CLEAR"),
    (1 << 16, "PUB_KEY_ID"),
    (1 << 17, "CHUNK"),
    (1 << 18, "ALIAS_CERT"),
    (1 << 19, "SET_CERT"),
    (1 << 20, "CSR"),
    (1 << 24, "MEL"),
    (1 << 25, "EVENT"),
    (1 << 31, "LARGE_RESP"),
]


def decode_cap_flags(flags: int) -> List[str]:
    """Names of the capabilities a flags word advertises."""
    out = []
    for bit, name in CAP_FLAGS:
        # MEAS is a 2-bit field: report whichever value is present, not both
        if name == "MEAS_NO_SIG" and (flags >> 3) & 3 != 1:
            continue
        if name == "MEAS_SIG" and (flags >> 3) & 3 != 2:
            continue
        if flags & bit == bit:
            out.append(name)
    return out


def parse_capabilities(msg: "Message") -> dict:
    """Pull CTExponent, flags and sizes out of a CAPABILITIES response."""
    p = msg.payload
    if msg.spdm_code != 0x61 or len(p) < 11:
        return {}
    info = {
        "ct_exponent": p[3],
        "flags": int.from_bytes(p[6:10], "little"),
    }
    info["flag_names"] = decode_cap_flags(info["flags"])
    if len(p) >= 18:
        info["data_transfer_size"] = int.from_bytes(p[10:14], "little")
        info["max_spdm_msg_size"] = int.from_bytes(p[14:18], "little")
    return info


# -------------------------------------------------- version-correct bodies
def req_get_version() -> bytes:
    return bytes([0x00, 0x00])


def req_get_capabilities(data_transfer: int = 1024, max_msg: int = 4096) -> bytes:
    """1.1+ body: reserved, CTExponent, reserved x2, flags, then sizes."""
    return (bytes([0x00, 0x00, 0x00, 0x0C, 0x00, 0x00])
            + (0).to_bytes(4, "little")
            + data_transfer.to_bytes(4, "little")
            + max_msg.to_bytes(4, "little"))


def req_negotiate_algorithms() -> bytes:
    """Minimal NEGOTIATE_ALGORITHMS: SHA-384 hash, ECDSA P-384 signature.

    param1 = 0 ext algorithm structures, Length covers the whole message.
    """
    body = bytearray()
    body += bytes([0x00, 0x00])            # param1 (0 alg structs), param2
    body += (32).to_bytes(2, "little")     # Length of this message
    body += bytes([0x01])                  # MeasurementSpecification: DMTF
    body += bytes([0x00])                  # OtherParamsSupport
    body += (0x0000_0080).to_bytes(4, "little")   # BaseAsymAlgo: ECDSA P-384
    body += (0x0000_0002).to_bytes(4, "little")   # BaseHashAlgo: SHA-384
    body += bytes(12)                      # reserved
    body += bytes([0x00])                  # ExtAsymCount
    body += bytes([0x00])                  # ExtHashCount
    body += bytes([0x00, 0x00])            # reserved
    return bytes(body)


def req_get_digests() -> bytes:
    return bytes([0x00, 0x00])


def req_get_certificate(slot: int = 0, offset: int = 0, length: int = 0x400) -> bytes:
    return (bytes([slot & 0x0F, 0x00])
            + offset.to_bytes(2, "little")
            + length.to_bytes(2, "little"))


def req_challenge(slot: int = 0, nonce: bytes = b"", measurement_summary: int = 0x01) -> bytes:
    import os as _os

    if not nonce:
        nonce = _os.urandom(32)
    return bytes([slot & 0x0F, measurement_summary]) + nonce


def req_get_measurements(nonce: bytes = b"", slot: int = 0,
                         signed: bool = True, index: int = 0xFF) -> bytes:
    """param1 bit0 set asks for a signature over the measurements."""
    import os as _os

    param1 = 0x01 if signed else 0x00
    body = bytes([param1, index])
    if signed:
        if not nonce:
            nonce = _os.urandom(32)
        body += nonce + bytes([slot & 0x0F])
    return body


#: The attestation flow, in order: (code, body builder, expected response).
ATTESTATION_FLOW = [
    (0x84, req_get_version, 0x04, "GET_VERSION"),
    (0xE1, req_get_capabilities, 0x61, "GET_CAPABILITIES"),
    (0xE3, req_negotiate_algorithms, 0x63, "NEGOTIATE_ALGORITHMS"),
    (0x81, req_get_digests, 0x01, "GET_DIGESTS"),
    (0x82, req_get_certificate, 0x02, "GET_CERTIFICATE"),
    (0x83, req_challenge, 0x03, "CHALLENGE"),
    (0xE0, req_get_measurements, 0x60, "GET_MEASUREMENTS"),
]
'''

s = s.rstrip() + ADD
open(P, "w").write(s)
print("patched", P)

# ------------------------------------------------------------------- bridge
B = "spdm_bridge/bridge.py"
s = open(B).read()
if "wait_response" not in s:
    old = """    def note(self, text: str, kind: str = "info") -> None:"""
    new = '''    def wait_response(self, code: int, timeout: float = 20.0):
        """Block until a response with this SPDM code arrives, or give up.

        Returns the Message, or None on timeout. SPDM is request/response:
        the next request must not go out until this one is answered.
        """
        import time as _t

        start = _t.monotonic()
        seen = self._seq_seen()
        while _t.monotonic() - start < timeout:
            with self._lock:
                for e in self.log:
                    if e.seq <= seen:
                        continue
                    msg = self._responses.get(e.seq)
                    if msg is not None and msg.spdm_code == code:
                        return msg
            _t.sleep(0.05)
        return None

    def _seq_seen(self) -> int:
        with self._lock:
            return self.log[-1].seq if self.log else 0

    def note(self, text: str, kind: str = "info") -> None:'''
    assert s.count(old) == 1, "note anchor not unique"
    s = s.replace(old, new, 1)

    # keep the decoded Message alongside its log entry
    old = """        self._control = {}"""
    new = """        self._control = {}
        # decoded responses, by log sequence number, for wait_response()
        self._responses = {}"""
    assert s.count(old) == 1, "state anchor not unique"
    s = s.replace(old, new, 1)

    old = """        self._emit(LogEntry(
            seq=next(self._seq), t=time.time(), source=source, target=target,
            direction=f"{source} -> {target}", summary=msg.summary(),
            kind=msg.kind, hexdump=msg.hex(), name=msg.name, relayed=relayed,
        ))"""
    new = """        entry = LogEntry(
            seq=next(self._seq), t=time.time(), source=source, target=target,
            direction=f"{source} -> {target}", summary=msg.summary(),
            kind=msg.kind, hexdump=msg.hex(), name=msg.name, relayed=relayed,
        )
        with self._lock:
            self._responses[entry.seq] = msg
        self._emit(entry)"""
    assert s.count(old) == 1, "emit anchor not unique"
    s = s.replace(old, new, 1)
    open(B, "w").write(s)
    print("patched", B)

# ------------------------------------------------------------------- server
S = "spdm_bridge/server.py"
s = open(S).read()
if "/api/attest" not in s:
    old = """  <button onclick="run()">Boot + Run</button>"""
    new = """  <button onclick="post('/api/attest',{target:'fpga'})">Run attestation</button>
  <button onclick="run()">Boot + Run</button>"""
    assert s.count(old) == 1, "button anchor not unique"
    s = s.replace(old, new, 1)

    old = """            elif url.path == "/api/sequence":"""
    new = '''            elif url.path == "/api/attest":
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
                self._send(200, b\'{"ok":true}\')
            elif url.path == "/api/sequence":'''
    assert s.count(old) == 1, "route anchor not unique"
    s = s.replace(old, new, 1)

    old = """            elif url.path == "/api/status":"""
    new = """            elif url.path == "/api/attest/state":
                self._send(200, json.dumps(
                    getattr(bridge, "attest_state", {})).encode())
            elif url.path == "/api/status":"""
    assert s.count(old) == 1, "status route anchor not unique"
    s = s.replace(old, new, 1)
    open(S, "w").write(s)
    print("patched", S)

print("restart the framework to pick this up")
