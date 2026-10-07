#!/usr/bin/env python3
"""
i2c_slave_listen.py - make the Pi an I2C slave and show what a master sends it.

Runs ON THE PI. Use this when the other board drives the bus, which the Pi's
normal I2C controller cannot handle: /dev/i2c-1 (i2c-bcm2835) is master-only.

The Pi has a second peripheral, the BSC slave, which CAN be addressed. It is
on different pins:

    GPIO 18 (pin 12) = SDA
    GPIO 19 (pin 35) = SCL
    plus a shared ground

So the Tektagon's I2C lines must move to those pins for this to see anything.

    sudo pigpiod                      # once, if not already running
    python3 i2c_slave_listen.py --addr 0x38
    python3 i2c_slave_listen.py --addr 0x38 --decode   # try to parse as MCTP

Whatever arrives is printed as hex, with a best-effort MCTP/SPDM decode so you
can tell immediately whether the framing matches what we expect.

Needs pigpio:  sudo apt install pigpio python3-pigpio
"""
import argparse
import sys
import time

try:
    import pigpio
except ImportError:
    sys.exit("sudo apt install python3-pigpio")

MSG_TYPES = {0x00: "MCTP-control", 0x01: "PLDM", 0x05: "SPDM", 0x06: "SECURED-SPDM"}
SPDM = {
    0x84: "GET_VERSION", 0xE1: "GET_CAPABILITIES", 0xE3: "NEGOTIATE_ALGORITHMS",
    0x81: "GET_DIGESTS", 0x82: "GET_CERTIFICATE", 0x83: "CHALLENGE",
    0xE0: "GET_MEASUREMENTS", 0x04: "VERSION", 0x61: "CAPABILITIES",
    0x63: "ALGORITHMS", 0x01: "DIGESTS", 0x02: "CERTIFICATE",
    0x03: "CHALLENGE_AUTH", 0x60: "MEASUREMENTS", 0x7F: "ERROR",
}


def hexs(d):
    return " ".join(f"{b:02X}" for b in d)


def decode(data):
    """Best-effort read of a buffer as MCTP over I2C (DSP0237) or raw MCTP."""
    out = []
    if len(data) < 4:
        return ["too short to decode"]

    # DSP0237: dest, cmd 0x0f, byte count, source, then the MCTP packet
    if data[0] == 0x0F:
        count = data[1]
        out.append(f"SMBus block: cmd 0x0F, byte count {count}")
        body = data[2:2 + count]
    else:
        body = data

    if len(body) >= 4:
        hdr_ver = body[0] & 0x0F
        out.append(f"MCTP hdr v{hdr_ver} dest {body[1]:#04x} src {body[2]:#04x}")
        f = body[3]
        out.append(f"  SOM={f >> 7 & 1} EOM={f >> 6 & 1} seq={f >> 4 & 3} "
                   f"TO={f >> 3 & 1} tag={f & 7}")
        if len(body) >= 5:
            mt = body[4] & 0x7F
            out.append(f"  type {mt:#04x} ({MSG_TYPES.get(mt, 'unknown')})")
            if mt == 0x05 and len(body) >= 7:
                out.append(f"  SPDM 1.{body[5] & 0xF} "
                           f"{body[6]:#04x} {SPDM.get(body[6], 'UNKNOWN')}")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=0x38,
                    help="slave address for the Pi to answer on (default 0x38)")
    ap.add_argument("--decode", action="store_true",
                    help="try to parse each message as MCTP/SPDM")
    ap.add_argument("--reply", help="hex bytes to queue as a response, e.g. '7E 01'")
    args = ap.parse_args()

    pi = pigpio.pi()
    if not pi.connected:
        sys.exit("pigpio daemon not running - try: sudo pigpiod")

    # open the BSC peripheral in I2C slave mode on GPIO 18/19
    pi.bsc_i2c(args.addr)  # opens and clears
    print(f"listening as I2C slave at {args.addr:#04x} on GPIO 18 (SDA) / 19 (SCL)")
    print("wire the master's SDA/SCL to those pins, plus a shared ground")
    print("Ctrl+C to stop\n")

    reply = bytes.fromhex(args.reply.replace(" ", "")) if args.reply else b""
    total = 0

    try:
        while True:
            status, count, data = pi.bsc_i2c(args.addr, reply) if reply \
                else pi.bsc_i2c(args.addr)
            if count:
                total += count
                stamp = time.strftime("%H:%M:%S")
                print(f"{stamp}  {count} byte(s)  {hexs(data)}")
                if args.decode:
                    for line in decode(bytes(data)):
                        print(f"           {line}")
                print()
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        print(f"\nstopping; {total} byte(s) received")
    finally:
        pi.bsc_i2c(0)  # close the peripheral
        pi.stop()


if __name__ == "__main__":
    main()
