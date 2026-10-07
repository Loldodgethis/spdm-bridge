#!/usr/bin/env python3
"""
uart_tx.py - push bytes into the MCU's UART receive register on the FPGA.

Runs ON THE BOARD as root, while MCU firmware is running.

Registers in the FPGA wrapper (uio0 map 0, base 0xa4010000):
    0x12c  mci_generic_input_wires[0]  host -> MCU
           [7:0] byte, [8] valid, [23:16] sequence counter
    0x130  mci_generic_input_wires[1]  MCU -> host acknowledgement
           [7:0] sequence number the MCU just consumed

Default mode waits for each byte to be acknowledged before sending the next,
so nothing is lost no matter how the MCU is scheduled. It needs firmware with
patch_echo_ack.py applied. Use --no-ack for the old fire-and-forget behaviour.

Examples:
    python3 uart_tx.py --text "hello"
    python3 uart_tx.py --file /root/test.txt
    python3 uart_tx.py --file /root/test.txt --dump      # per-byte detail
    python3 uart_tx.py --text hi --no-ack --delay 0.3    # no flow control
    python3 uart_tx.py --probe                           # read registers only
"""
import argparse
import mmap
import os
import struct
import sys
import time

UIO = "/dev/uio0"
MAP = 0
TX_OFFSET = 0x12C
ACK_OFFSET = 0x130
VALID = 1 << 8


def open_regs():
    size_path = f"/sys/class/uio/{os.path.basename(UIO)}/maps/map{MAP}/size"
    try:
        size = int(open(size_path).read(), 16)
    except OSError:
        sys.exit(f"cannot read {size_path} - is the bitstream loaded?")
    fd = os.open(UIO, os.O_RDWR | os.O_SYNC)
    mm = mmap.mmap(fd, size, mmap.MAP_SHARED,
                   mmap.PROT_READ | mmap.PROT_WRITE,
                   offset=MAP * mmap.PAGESIZE)
    return fd, mm


def rd(mm, off):
    return struct.unpack_from("<I", mm, off)[0]


def wr(mm, off, value):
    struct.pack_into("<I", mm, off, value)


def describe(v):
    return (f"0x{v:08x}  byte=0x{v & 0xFF:02x} "
            f"valid={(v >> 8) & 1} seq={(v >> 16) & 0xFF}")


def send(mm, data, args):
    seq = (rd(mm, TX_OFFSET) >> 16) & 0xFF
    sent = acked = lost = 0
    started = time.monotonic()

    for b in data:
        seq = (seq + 1) & 0xFF
        wr(mm, TX_OFFSET, (seq << 16) | VALID | b)
        sent += 1
        ch = chr(b) if 32 <= b < 127 else "."

        if args.no_ack:
            if args.dump:
                print(f"sent '{ch}' seq={seq}")
            time.sleep(args.delay)
            continue

        deadline = time.monotonic() + args.timeout
        ok = False
        while time.monotonic() <= deadline:
            if (rd(mm, ACK_OFFSET) & 0xFF) == seq:
                ok = True
                break
            time.sleep(0.001)

        if ok:
            acked += 1
            if args.dump:
                print(f"sent '{ch}' seq={seq} -> acked")
        else:
            lost += 1
            if args.dump:
                print(f"sent '{ch}' seq={seq} -> NO ACK after {args.timeout}s")

    elapsed = time.monotonic() - started
    if args.no_ack:
        print(f"sent {sent} byte(s) in {elapsed:.1f}s, no flow control")
    else:
        rate = acked / elapsed if elapsed > 0 else 0
        print(f"sent {sent} byte(s) in {elapsed:.1f}s: "
              f"{acked} acked, {lost} timed out ({rate:.1f} bytes/sec)")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--text", help="string to send (a newline is appended)")
    g.add_argument("--file", help="file whose bytes to send")
    ap.add_argument("--probe", action="store_true", help="read the registers and exit")
    ap.add_argument("--dump", action="store_true", help="print each byte and its result")
    ap.add_argument("--no-ack", action="store_true",
                    help="don't wait for acknowledgement (old behaviour)")
    ap.add_argument("--delay", type=float, default=0.2,
                    help="seconds between bytes, --no-ack only (default 0.2)")
    ap.add_argument("--timeout", type=float, default=5.0,
                    help="seconds to wait for each acknowledgement (default 5)")
    args = ap.parse_args()

    fd, mm = open_regs()
    try:
        if args.probe or not (args.text or args.file):
            ack = rd(mm, ACK_OFFSET)
            print("tx :", describe(rd(mm, TX_OFFSET)))
            print(f"ack: 0x{ack:08x}  seq={ack & 0xFF}")
            if not (args.text or args.file):
                return

        data = (args.text + "\n").encode() if args.text else open(args.file, "rb").read()
        if not data:
            sys.exit("nothing to send")
        send(mm, data, args)
    finally:
        mm.close()
        os.close(fd)


if __name__ == "__main__":
    main()
