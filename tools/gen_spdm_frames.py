#!/usr/bin/env python3
"""
gen_spdm_frames.py - build the SPDM request files for the mock attestation.

Run anywhere, then copy the output directory to the board:
    python3 gen_spdm_frames.py --out spdm
    scp -r spdm root@<board-ip>:/root/

Each file holds ONE framed SPDM request:

    0x7E | rev | len | MCTP packet | FCS-16 | 0x7E       (DSP0253 serial framing)
                       ^ 4-byte MCTP header + msg type 0x05 + SPDM message

0x7E and 0x7D inside the frame are escaped as 0x7D 0x5E / 0x7D 0x5D.

Keep these SMALL. The channel runs about 4 bytes/sec and the firmware is only
up ~35s per test run, so the whole set must stay well under ~140 bytes.
"""
import argparse
import os

FLAG = 0x7E
ESC = 0x7D
REV = 0x01
MSG_TYPE_SPDM = 0x05
DEST_EID = 0x10
SRC_EID = 0x08

# SPDM requests: (filename, code, name, payload after the 2-byte SPDM header)
REQUESTS = [
    ("01_get_version.bin", 0x84, "GET_VERSION", b"\x00\x00"),
    ("02_get_capabilities.bin", 0xE1, "GET_CAPABILITIES", b"\x00\x00"),
    ("03_get_digests.bin", 0x81, "GET_DIGESTS", b"\x00\x00"),
    ("04_challenge.bin", 0x83, "CHALLENGE", b"\x00\x00\x01\x02\x03\x04"),
]
SPDM_VERSION = 0x10


def fcs16(data: bytes) -> int:
    fcs = 0xFFFF
    for b in data:
        v = (fcs ^ b) & 0xFF
        for _ in range(8):
            v = (v >> 1) ^ 0x8408 if v & 1 else v >> 1
        fcs = (fcs >> 8) ^ v
    return (~fcs) & 0xFFFF


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (FLAG, ESC):
            out += bytes([ESC, b ^ 0x20])
        else:
            out.append(b)
    return bytes(out)


def build_frame(code: int, payload: bytes) -> bytes:
    # MCTP transport header: version, dest EID, src EID, SOM|EOM|seq|TO|tag
    mctp = bytes([0x01, DEST_EID, SRC_EID, 0b1100_1000])
    mctp += bytes([MSG_TYPE_SPDM, SPDM_VERSION, code]) + payload
    covered = bytes([REV, len(mctp)]) + mctp
    fcs = fcs16(covered)
    body = covered + bytes([(fcs >> 8) & 0xFF, fcs & 0xFF])
    return bytes([FLAG]) + escape(body) + bytes([FLAG])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="spdm", help="output directory (default: spdm)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    total = 0
    for fname, code, name, payload in REQUESTS:
        frame = build_frame(code, payload)
        path = os.path.join(args.out, fname)
        with open(path, "wb") as f:
            f.write(frame)
        total += len(frame)
        print(f"{path:34s} {len(frame):3d} bytes  {name} (0x{code:02X})")
        print(f"{'':34s}     {' '.join(f'{b:02X}' for b in frame)}")

    print(f"\n{len(REQUESTS)} frames, {total} bytes total "
          f"(~{total / 4:.0f}s at 4 bytes/sec)")


if __name__ == "__main__":
    main()
