#!/usr/bin/env python3
"""
i2c_probe.py - work out how a device on the I2C bus wants to be talked to.

Runs ON THE PI. Tries every plausible access pattern against each address and
reports which ones the device accepts, so the transport can be written to match
instead of guessed at.

    python3 i2c_probe.py                    # probe every address found
    python3 i2c_probe.py --addr 0x38        # just one
    python3 i2c_probe.py --addr 0x38 --send # also send an SPDM GET_VERSION

Patterns tried, read first (harmless) then write:

    raw read            read N bytes with no command byte
    SMBus byte read     read_byte_data(reg) for a few low registers
    SMBus block read    read_block_data(0x0f)   - MCTP over I2C command code
    quick write         zero-length write, just checks the address ACKs
    MCTP block write    SMBus block write at 0x0f with a framed request
    raw write           same payload with no command byte

Needs smbus2:  pip install smbus2
"""
import argparse
import sys
import time

try:
    from smbus2 import SMBus, i2c_msg
except ImportError:
    sys.exit("pip install smbus2")

MCTP_CMD = 0x0F


def fcs16(data):
    fcs = 0xFFFF
    for b in data:
        v = (fcs ^ b) & 0xFF
        for _ in range(8):
            v = (v >> 1) ^ 0x8408 if v & 1 else v >> 1
        fcs = (fcs >> 8) ^ v
    return (~fcs) & 0xFFFF


def spdm_get_version_mctp():
    """MCTP packet carrying SPDM GET_VERSION, no serial framing."""
    return bytes([0x01, 0x10, 0x08, 0xC8, 0x05, 0x10, 0x84, 0x00, 0x00])


def hexs(data):
    return " ".join(f"{b:02X}" for b in data)


def interesting(data):
    """Does this look like a real reply rather than idle bus noise?"""
    if not data:
        return False
    if all(b == 0x00 for b in data) or all(b == 0xFF for b in data):
        return False
    return True


def scan(bus_no):
    found = []
    with SMBus(bus_no) as bus:
        for addr in range(0x03, 0x78):
            try:
                bus.i2c_rdwr(i2c_msg.read(addr, 1))
                found.append(addr)
            except OSError:
                pass
    return found


def probe_reads(bus, addr, verbose):
    results = {}

    # 1. raw read, no command byte
    for n in (1, 8, 32, 64):
        try:
            msg = i2c_msg.read(addr, n)
            bus.i2c_rdwr(msg)
            data = bytes(msg)
            results[f"raw read {n}B"] = (True, data)
        except OSError as e:
            results[f"raw read {n}B"] = (False, str(e))

    # 2. SMBus byte reads on low registers
    for reg in (0x00, 0x01, 0x02, 0x0F):
        try:
            v = bus.read_byte_data(addr, reg)
            results[f"byte read reg {reg:#04x}"] = (True, bytes([v]))
        except OSError as e:
            results[f"byte read reg {reg:#04x}"] = (False, str(e))

    # 3. SMBus block read at the MCTP command code
    try:
        data = bytes(bus.read_block_data(addr, MCTP_CMD))
        results[f"block read {MCTP_CMD:#04x}"] = (True, data)
    except OSError as e:
        results[f"block read {MCTP_CMD:#04x}"] = (False, str(e))

    return results


def probe_writes(bus, addr, payload, verbose):
    results = {}

    # quick write: does the address ACK at all
    try:
        bus.i2c_rdwr(i2c_msg.write(addr, b""))
        results["quick write"] = (True, b"")
    except OSError as e:
        results["quick write"] = (False, str(e))

    # MCTP-style SMBus block write: cmd 0x0f, byte count, payload
    try:
        body = bytes([MCTP_CMD, len(payload)]) + payload
        bus.i2c_rdwr(i2c_msg.write(addr, body))
        results["MCTP block write"] = (True, body)
    except OSError as e:
        results["MCTP block write"] = (False, str(e))

    # raw write, no command byte
    try:
        bus.i2c_rdwr(i2c_msg.write(addr, payload))
        results["raw write"] = (True, payload)
    except OSError as e:
        results["raw write"] = (False, str(e))

    return results


def report(title, results, verbose):
    print(f"  {title}")
    for name, (ok, data) in results.items():
        if ok:
            shown = hexs(data) if isinstance(data, bytes) else data
            flag = "  <-- looks like data" if isinstance(data, bytes) and interesting(data) else ""
            print(f"    OK    {name:24s} {shown[:60]}{flag}")
        elif verbose:
            print(f"    fail  {name:24s} {data}")
    if not verbose:
        fails = sum(1 for ok, _ in results.values() if not ok)
        if fails:
            print(f"    ({fails} pattern(s) refused; --verbose to see why)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", type=int, default=1)
    ap.add_argument("--addr", type=lambda x: int(x, 0), action="append",
                    help="address to probe (repeatable); default: scan")
    ap.add_argument("--send", action="store_true",
                    help="also send an SPDM GET_VERSION and re-read after")
    ap.add_argument("--verbose", action="store_true", help="show failures too")
    args = ap.parse_args()

    addrs = args.addr or scan(args.bus)
    if not addrs:
        sys.exit("no devices responded on the bus")
    print(f"bus {args.bus}, probing: {', '.join(f'{a:#04x}' for a in addrs)}\n")

    payload = spdm_get_version_mctp()
    with SMBus(args.bus) as bus:
        for addr in addrs:
            print(f"=== {addr:#04x} ===")
            report("reads:", probe_reads(bus, addr, args.verbose), args.verbose)

            if args.send:
                report("writes:", probe_writes(bus, addr, payload, args.verbose),
                       args.verbose)
                time.sleep(0.3)
                print("  reads after sending a request:")
                after = probe_reads(bus, addr, args.verbose)
                hits = [(n, d) for n, (ok, d) in after.items()
                        if ok and isinstance(d, bytes) and interesting(d)]
                if hits:
                    for n, d in hits:
                        print(f"    REPLY {n:24s} {hexs(d)[:60]}")
                else:
                    print("    nothing came back - device likely replies as "
                          "master (see i2c_slave_listen.py)")
            print()

    print("Reading this: a pattern marked 'looks like data' is how the device")
    print("wants to be read. If every read is empty or refused but writes are")
    print("accepted, the device is a master and the Pi has to listen instead.")


if __name__ == "__main__":
    main()
