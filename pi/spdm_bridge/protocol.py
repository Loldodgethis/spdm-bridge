"""
protocol.py - SPDM over MCTP over DSP0253 serial framing.

Layering, outermost first:

    7E | rev | len | MCTP packet | FCS-16 | 7E      serial framing (DSP0253)
                     ^ 4-byte MCTP header + message type + SPDM message

Used by both endpoints of the bridge, so a frame decoded from the FPGA and one
decoded from the Tektagon come out in the same shape.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional

FLAG = 0x7E
ESCAPE = 0x7D
REV = 0x01

MSG_TYPE_CONTROL = 0x00
MSG_TYPE_PLDM = 0x01
MSG_TYPE_SPDM = 0x05
MSG_TYPES = {0x00: "MCTP-control", 0x01: "PLDM", 0x05: "SPDM", 0x06: "SECURED-SPDM"}

# SPDM request codes are 0x80+, responses are their code minus 0x80.
SPDM_REQUESTS = {
    0x84: "GET_VERSION",
    0xE1: "GET_CAPABILITIES",
    0xE3: "NEGOTIATE_ALGORITHMS",
    0x81: "GET_DIGESTS",
    0x82: "GET_CERTIFICATE",
    0x83: "CHALLENGE",
    0xE0: "GET_MEASUREMENTS",
}
SPDM_RESPONSES = {
    0x04: "VERSION",
    0x61: "CAPABILITIES",
    0x63: "ALGORITHMS",
    0x01: "DIGESTS",
    0x02: "CERTIFICATE",
    0x03: "CHALLENGE_AUTH",
    0x60: "MEASUREMENTS",
    0x7F: "ERROR",
}


def spdm_name(code: int) -> str:
    if code in SPDM_REQUESTS:
        return SPDM_REQUESTS[code]
    if code in SPDM_RESPONSES:
        return SPDM_RESPONSES[code]
    return f"UNKNOWN_{code:#04x}"


def is_request(code: int) -> bool:
    return code in SPDM_REQUESTS


# ----------------------------------------------------------------- FCS-16
def fcs16(data: bytes, fcs: int = 0xFFFF) -> int:
    """RFC 1662 FCS-16, as referenced by DSP0253."""
    for b in data:
        v = (fcs ^ b) & 0xFF
        for _ in range(8):
            v = (v >> 1) ^ 0x8408 if v & 1 else v >> 1
        fcs = (fcs >> 8) ^ v
    return (~fcs) & 0xFFFF


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (FLAG, ESCAPE):
            out += bytes([ESCAPE, b ^ 0x20])
        else:
            out.append(b)
    return bytes(out)


# ------------------------------------------------------------- MCTP packet
@dataclass
class Message:
    """One decoded MCTP packet, plus whatever we could read of its payload."""

    dest_eid: int
    src_eid: int
    som: bool
    eom: bool
    seq: int
    tag_owner: bool
    msg_tag: int
    msg_type: Optional[int] = None
    spdm_version: Optional[int] = None
    spdm_code: Optional[int] = None
    payload: bytes = b""
    raw: bytes = b""
    error: Optional[str] = None

    @property
    def type_name(self) -> str:
        if self.msg_type is None:
            return "?"
        return MSG_TYPES.get(self.msg_type, f"{self.msg_type:#04x}")

    @property
    def name(self) -> str:
        if self.error:
            return f"INVALID ({self.error})"
        if self.msg_type == MSG_TYPE_SPDM and self.spdm_code is not None:
            return spdm_name(self.spdm_code)
        return self.type_name

    @property
    def kind(self) -> str:
        """'request', 'response' or 'other' - drives colouring in the UI."""
        if self.error:
            return "error"
        if self.msg_type == MSG_TYPE_SPDM and self.spdm_code is not None:
            return "request" if is_request(self.spdm_code) else "response"
        return "other"

    def summary(self) -> str:
        if self.error:
            return f"invalid frame: {self.error}"
        bits = [f"{self.type_name}"]
        if self.spdm_code is not None:
            v = f"1.{self.spdm_version & 0xF}" if self.spdm_version is not None else "?"
            bits.append(f"SPDM {v} {self.name} ({self.spdm_code:#04x})")
        bits.append(f"EID {self.src_eid:#04x}->{self.dest_eid:#04x}")
        bits.append(f"tag {self.msg_tag}")
        return " | ".join(bits)

    def hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.raw)

    def raw_frame(self) -> bytes:
        """Re-frame this message for sending on to the other side."""
        return bytes([FLAG]) + escape(self.raw) + bytes([FLAG])


def build_frame(spdm_code: int, payload: bytes = b"\x00\x00", *,
                dest_eid: int = 0x10, src_eid: int = 0x08, msg_tag: int = 0,
                spdm_version: int = 0x10, msg_type: int = MSG_TYPE_SPDM) -> bytes:
    """Build one complete framed message ready to put on the wire."""
    flags = 0b1100_1000 | (msg_tag & 0x7)  # SOM|EOM|seq0|TO
    mctp = bytes([0x01, dest_eid, src_eid, flags, msg_type, spdm_version, spdm_code])
    mctp += payload
    covered = bytes([REV, len(mctp)]) + mctp
    fcs = fcs16(covered)
    body = covered + bytes([(fcs >> 8) & 0xFF, fcs & 0xFF])
    return bytes([FLAG]) + escape(body) + bytes([FLAG])


def parse_version_response(msg: "Message") -> List[int]:
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


def build_frame_raw(spdm_code: int, payload: bytes = b"\x00\x00", *,
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


def decode_frame(body: bytes) -> Message:
    """Decode one unescaped frame body (rev .. FCS) into a Message."""
    raw = body
    if len(body) < 5:
        return Message(0, 0, False, False, 0, False, 0, raw=raw,
                       error=f"too short ({len(body)} bytes)")
    # A frame whose first byte is an SPDM message type is the 1-byte-header
    # form used on the register channel; anything else is read as DSP0253.
    if body[0] & 0x7F in MSG_TYPES and body[0] != REV:
        return _decode_raw(body)

    rev, length = body[0], body[1]
    want = (body[-2] << 8) | body[-1]
    got = fcs16(body[:-2])
    if rev != REV:
        return Message(0, 0, False, False, 0, False, 0, raw=raw,
                       error=f"bad revision {rev:#04x}")
    if length != len(body) - 4:
        return Message(0, 0, False, False, 0, False, 0, raw=raw,
                       error=f"length {length} != {len(body) - 4}")
    if got != want:
        return Message(0, 0, False, False, 0, False, 0, raw=raw,
                       error=f"FCS {got:#06x} != {want:#06x}")

    mctp = body[2:-2]
    f = mctp[3]
    msg = Message(
        dest_eid=mctp[1], src_eid=mctp[2],
        som=bool(f & 0x80), eom=bool(f & 0x40), seq=(f >> 4) & 0x3,
        tag_owner=bool(f & 0x08), msg_tag=f & 0x7, raw=raw,
    )
    rest = mctp[4:]
    if rest:
        msg.msg_type = rest[0] & 0x7F
        msg.payload = rest[1:]
        if msg.msg_type == MSG_TYPE_SPDM and len(rest) >= 3:
            msg.spdm_version = rest[1]
            msg.spdm_code = rest[2]
    return msg


class FrameDecoder:
    """Feed raw bytes in any chunk size; yields Messages, good and bad."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._esc = False
        self.frames = 0
        self.errors = 0

    def feed(self, data: bytes) -> Iterator[Message]:
        for b in data:
            if b == FLAG:
                if self._in_frame and self._buf:
                    msg = decode_frame(bytes(self._buf))
                    if msg.error:
                        self.errors += 1
                    else:
                        self.frames += 1
                    yield msg
                self._in_frame = True
                self._buf.clear()
                self._esc = False
            elif not self._in_frame:
                continue
            elif self._esc:
                self._buf.append(b ^ 0x20)
                self._esc = False
            elif b == ESCAPE:
                self._esc = True
            else:
                self._buf.append(b)


# ------------------------------------------------- canned exchange for demos
REQUEST_SEQUENCE = [
    (0x84, b"\x00\x00"),                      # GET_VERSION
    (0xE1, b"\x00\x00"),                      # GET_CAPABILITIES
    (0x81, b"\x00\x00"),                      # GET_DIGESTS
    (0x83, b"\x00\x00\x01\x02\x03\x04"),      # CHALLENGE + short nonce
]

RESPONSE_FOR = {
    0x84: (0x04, b"\x00\x00\x10"),            # VERSION
    0xE1: (0x61, b"\x00\x00\x01"),            # CAPABILITIES
    0x81: (0x01, b"\x00\x01\xAB\xCD"),        # DIGESTS
    0x83: (0x03, b"\x00\x00\xDE\xAD\xBE\xEF"),  # CHALLENGE_AUTH
}


def canned_request_stream() -> bytes:
    return b"".join(build_frame(c, p) for c, p in REQUEST_SEQUENCE)


def response_to(msg: Message) -> Optional[bytes]:
    """Build the matching response frame for a request, if we know one."""
    if msg.error or msg.spdm_code is None:
        return None
    pair = RESPONSE_FOR.get(msg.spdm_code)
    if not pair:
        return None
    code, payload = pair
    return build_frame(code, payload, dest_eid=msg.src_eid, src_eid=msg.dest_eid,
                       msg_tag=msg.msg_tag)

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


def req_get_capabilities(data_transfer: int = 1024, max_msg: int = 1024) -> bytes:
    """1.1+ body: reserved, CTExponent, reserved x2, flags, then sizes."""
    return (bytes([0x00, 0x00, 0x00, 0x0C, 0x00, 0x00])
            + (0).to_bytes(4, "little")
            + data_transfer.to_bytes(4, "little")
            + max_msg.to_bytes(4, "little"))


def req_negotiate_algorithms() -> bytes:
    """NEGOTIATE_ALGORITHMS, 30-byte fixed prefix, no AlgStruct entries.

    Layout from the responder's NegotiateAlgorithmsReqBodyFixed:
        num_alg_struct, param2, length(2), measurement_spec,
        other_param_support, base_asym_algo(4), base_hash_algo(4),
        pqc_asym_algo(4), reserved1[8], ext_asym_count, ext_hash_count,
        reserved2, mel_spec. SIZE = 30.

    length counts the whole request including the 2-byte SPDM header,
    so 2 + 30 = 32 with no AlgStruct entries.
    """
    body = bytearray()
    body += bytes([0x00, 0x00])                    # num_alg_struct, param2
    body += (32).to_bytes(2, "little")             # length incl. SPDM header
    body += bytes([0x01])                          # measurement_spec: DMTF
    body += bytes([0x00])                          # other_param_support
    body += (0x0000_0080).to_bytes(4, "little")    # base_asym: ECDSA P-384
    body += (0x0000_0002).to_bytes(4, "little")    # base_hash: SHA-384
    body += (0).to_bytes(4, "little")              # pqc_asym_algo
    body += bytes(8)                               # reserved1
    body += bytes([0x00, 0x00])                    # ext_asym, ext_hash counts
    body += bytes([0x00, 0x00])                    # reserved2, mel_spec
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
