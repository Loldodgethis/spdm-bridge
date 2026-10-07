#!/usr/bin/env python3
"""
uart_packets.py - Build and parse MCTP-over-serial (DMTF DSP0253) frames.

Layering:
    [SPDM / PLDM / control message]
      -> MCTP packet(s)  (4-byte transport header + message-type byte on first packet)
        -> Serial frame  (0x7E | rev | len | MCTP packet | FCS-16 | 0x7E, with 0x7D escaping)

NOTE: Verify FCS byte order and coverage against the DSP0253 spec before
using against real hardware. The self-test only proves encode/decode symmetry.

Usage:
    python3 uart_packets.py                      # self-test + hex dump demo
    python3 uart_packets.py --port /dev/ttyUSB1  # also send the demo frames
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Iterator, List, Optional

# ---------------- Constants ----------------
FRAME_FLAG = 0x7E
ESCAPE = 0x7D
SERIAL_REVISION = 0x01
MCTP_HDR_VERSION = 0x01
BASELINE_MTU = 64  # bytes of MCTP packet (header + payload)

MSG_TYPE_CONTROL = 0x00
MSG_TYPE_PLDM = 0x01
MSG_TYPE_SPDM = 0x05


# ---------------- FCS-16 (RFC 1662, as referenced by DSP0253) ----------------
def _build_fcs_table() -> List[int]:
    table = []
    for b in range(256):
        v = b
        for _ in range(8):
            v = (v >> 1) ^ 0x8408 if v & 1 else v >> 1
        table.append(v)
    return table


_FCS_TABLE = _build_fcs_table()


def fcs16(data: bytes, fcs: int = 0xFFFF) -> int:
    for b in data:
        fcs = (fcs >> 8) ^ _FCS_TABLE[(fcs ^ b) & 0xFF]
    return (~fcs) & 0xFFFF


# ---------------- Byte stuffing ----------------
def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (FRAME_FLAG, ESCAPE):
            out += bytes([ESCAPE, b ^ 0x20])
        else:
            out.append(b)
    return bytes(out)


# ---------------- MCTP packet ----------------
@dataclass
class MctpPacket:
    dest_eid: int
    src_eid: int
    som: bool
    eom: bool
    seq: int          # 0-3
    tag_owner: bool
    msg_tag: int      # 0-7
    payload: bytes    # includes message-type byte on the SOM packet

    def to_bytes(self) -> bytes:
        flags = (
            (int(self.som) << 7)
            | (int(self.eom) << 6)
            | ((self.seq & 0x3) << 4)
            | (int(self.tag_owner) << 3)
            | (self.msg_tag & 0x7)
        )
        return bytes([MCTP_HDR_VERSION & 0x0F, self.dest_eid, self.src_eid, flags]) + self.payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> "MctpPacket":
        if len(raw) < 4:
            raise ValueError("MCTP packet shorter than header")
        f = raw[3]
        return cls(
            dest_eid=raw[1], src_eid=raw[2],
            som=bool(f & 0x80), eom=bool(f & 0x40), seq=(f >> 4) & 0x3,
            tag_owner=bool(f & 0x08), msg_tag=f & 0x7, payload=raw[4:],
        )

    @property
    def msg_type(self) -> Optional[int]:
        return self.payload[0] & 0x7F if self.som and self.payload else None


def build_mctp_packets(msg_type: int, message: bytes, dest_eid: int, src_eid: int,
                       msg_tag: int = 0, tag_owner: bool = True,
                       mtu: int = BASELINE_MTU) -> List[MctpPacket]:
    """Fragment one message into MCTP packets that fit the MTU."""
    body = bytes([msg_type & 0x7F]) + message
    chunk = mtu - 4
    pieces = [body[i:i + chunk] for i in range(0, len(body), chunk)] or [b""]
    return [
        MctpPacket(dest_eid, src_eid, som=(i == 0), eom=(i == len(pieces) - 1),
                   seq=i & 0x3, tag_owner=tag_owner, msg_tag=msg_tag, payload=p)
        for i, p in enumerate(pieces)
    ]


# ---------------- Serial framing ----------------
def encode_frame(pkt: MctpPacket) -> bytes:
    mctp = pkt.to_bytes()
    if len(mctp) > 255:
        raise ValueError("MCTP packet too large for 1-byte count")
    covered = bytes([SERIAL_REVISION, len(mctp)]) + mctp
    fcs = fcs16(covered)
    trailer = bytes([(fcs >> 8) & 0xFF, fcs & 0xFF])  # VERIFY byte order vs DSP0253
    return bytes([FRAME_FLAG]) + escape(covered + trailer) + bytes([FRAME_FLAG])


class FrameDecoder:
    """Feed raw UART bytes in any chunk size; yields decoded MctpPackets."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._in_frame = False
        self._esc = False
        self.errors = 0

    def feed(self, data: bytes) -> Iterator[MctpPacket]:
        for b in data:
            if b == FRAME_FLAG:
                if self._in_frame and self._buf:
                    pkt = self._finish()
                    if pkt:
                        yield pkt
                    self._in_frame = False  # a closing flag may double as next opener
                else:
                    self._in_frame = True
                self._buf.clear()
                self._esc = False
                if not self._in_frame:
                    self._in_frame = True
                continue
            if not self._in_frame:
                continue
            if self._esc:
                self._buf.append(b ^ 0x20)
                self._esc = False
            elif b == ESCAPE:
                self._esc = True
            else:
                self._buf.append(b)

    def _finish(self) -> Optional[MctpPacket]:
        raw = bytes(self._buf)
        if len(raw) < 4 or raw[0] != SERIAL_REVISION or raw[1] != len(raw) - 4:
            self.errors += 1
            return None
        covered, got = raw[:-2], (raw[-2] << 8) | raw[-1]
        if fcs16(covered) != got:
            self.errors += 1
            return None
        return MctpPacket.from_bytes(covered[2:])


# ---------------- Helpers ----------------
def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def self_test() -> None:
    msg = bytes([0x12, 0x84, 0x00, 0x00]) + bytes(range(0x70, 0x90))  # includes 0x7D/0x7E
    pkts = build_mctp_packets(MSG_TYPE_SPDM, msg, dest_eid=0x10, src_eid=0x08, mtu=24)
    wire = b"".join(encode_frame(p) for p in pkts)

    dec = FrameDecoder()
    out: List[MctpPacket] = []
    for i in range(0, len(wire), 7):  # simulate chunked UART reads
        out.extend(dec.feed(wire[i:i + 7]))

    assert dec.errors == 0, dec.errors
    assert len(out) == len(pkts)
    reassembled = b"".join(p.payload for p in out)
    assert reassembled[0] == MSG_TYPE_SPDM and reassembled[1:] == msg
    print(f"self-test OK: {len(pkts)} packets, {len(wire)} bytes on wire")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial device to send demo frames to")
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    self_test()

    # Demo: SPDM GET_VERSION request (SPDM 1.0 header: version 0x10, code 0x84)
    get_version = bytes([0x10, 0x84, 0x00, 0x00])
    frames = [encode_frame(p) for p in
              build_mctp_packets(MSG_TYPE_SPDM, get_version, dest_eid=0x10, src_eid=0x08)]
    for f in frames:
        print("frame:", hexdump(f))

    if args.port:
        import serial  # pip install pyserial
        with serial.Serial(args.port, args.baud, timeout=1) as s:
            for f in frames:
                s.write(f)
            print(f"sent {len(frames)} frame(s) to {args.port}")


if __name__ == "__main__":
    main()
