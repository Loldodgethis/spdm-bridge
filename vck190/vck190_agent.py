#!/usr/bin/env python3
"""
vck190_agent.py - bridge USB gadget serial to the MCU, and boot firmware on demand.

Runs ON THE VCK190 as root, ideally as a systemd service so it is always up.

    Pi  ->  /dev/ttyACM0  ==USB==  /dev/ttyGS0  ->  this agent
                                                        |
                                           FPGA wrapper registers
                                                        |
                                                  Caliptra MCU

Two jobs:

1. Byte pump. Serial bytes go to the MCU and MCU bytes come back, each side
   waiting for the other's acknowledgement, so nothing is lost regardless of
   scheduling.

2. Control channel. The MCU only lives while a test runs, so the Pi can ask
   for one over the same USB link. Control lines sit OUTSIDE SPDM frames and
   start with '!', so they never collide with traffic:

       !BOOT     start the firmware test locally (no laptop needed)
       !STATUS   reply !UP or !DOWN
       !STOP     kill a running test
       !PING     reply !PONG

   The agent answers with !BOOTING, !READY (MCU is answering), !UP, !DOWN,
   !BUSY, !EXIT <code>.

Registers:
    0xa401012c  host -> MCU   [7:0] byte [8] valid [23:16] seq [31:24] ack
    0xa4010130  MCU -> host   [7:0] ack  [15:8] byte [23:16] seq

Usage:
    python3 vck190_agent.py                 # serve on /dev/ttyGS0
    python3 vck190_agent.py --verbose
    python3 vck190_agent.py --selftest      # send one GET_VERSION, print reply
    python3 vck190_agent.py --boot          # boot firmware and exit
"""
import argparse
import mmap
import os
import struct
import subprocess
import sys
import time

UIO = "/dev/uio0"
MAP = 0
RX_OFFSET = 0x12C
RET_OFFSET = 0x130
VALID = 1 << 8
RET_TOP = 0xC000_0000

DEFAULT_TEST = ("test_mctp_capsule_loopback::test::test_mctp_capsule_loopback")
BOOT_CMD = (
    "cd {repo} && sudo CPTRA_FIRMWARE_BUNDLE=$HOME/all-fw.zip cargo-nextest nextest run "
    "--workspace-remap=. --archive-file $HOME/caliptra-test-binaries.tar.zst "
    "--no-capture --no-fail-fast --status-level=all --profile=nightly "
    '-E "package(caliptra-mcu-tests-integration) and test(={test})"'
)


class Regs:
    def __init__(self, path=UIO, map_index=MAP):
        size_path = f"/sys/class/uio/{os.path.basename(path)}/maps/map{map_index}/size"
        try:
            size = int(open(size_path).read(), 16)
        except OSError:
            sys.exit(f"cannot read {size_path} - is the bitstream loaded?")
        self.fd = os.open(path, os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE,
                            offset=map_index * mmap.PAGESIZE)

    def rd(self, off):
        return struct.unpack_from("<I", self.mm, off)[0]

    def wr(self, off, value):
        struct.pack_into("<I", self.mm, off, value & 0xFFFFFFFF)

    def close(self):
        self.mm.close()
        os.close(self.fd)


class Agent:
    def __init__(self, regs, verbose=False, timeout=1.0, retries=4):
        self.r = regs
        self.verbose = verbose
        self.timeout = timeout
        self.retries = max(1, retries)
        self.tx_seq = (self.r.rd(RX_OFFSET) >> 16) & 0xFF
        self.last_mcu_seq = (self.r.rd(RET_OFFSET) >> 16) & 0xFF
        self.mcu_ack = self.last_mcu_seq
        self.sent = self.received = self.lost = 0
        # bytes drained while we were busy sending; must not be dropped
        self.pending = bytearray()

    # ---------------------------------------------------------- host -> MCU
    def _rx_word(self, byte, seq):
        return ((self.mcu_ack & 0xFF) << 24) | ((seq & 0xFF) << 16) | VALID | byte

    def to_mcu(self, data, retries=None):
        """Write bytes to the MCU; a byte is re-presented until taken."""
        retries = self.retries if retries is None else retries
        ok = 0
        for b in data:
            self.tx_seq = (self.tx_seq + 1) & 0xFF
            acked = False
            for _ in range(retries):
                self.r.wr(RX_OFFSET, self._rx_word(b, self.tx_seq))
                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    if (self.r.rd(RET_OFFSET) & 0xFF) == self.tx_seq:
                        acked = True
                        break
                    self.drain_mcu()
                    time.sleep(0.0005)
                if acked:
                    break
            if acked:
                ok += 1
                self.sent += 1
                if self.verbose:
                    print(f"  -> MCU {b:#04x} acked (seq {self.tx_seq})", flush=True)
            else:
                self.lost += 1
                if self.verbose:
                    print(f"  -> MCU {b:#04x} not acked", flush=True)
        return ok

    # ---------------------------------------------------------- MCU -> host
    def drain_mcu(self):
        out = bytearray()
        while True:
            v = self.r.rd(RET_OFFSET)
            if v & 0xC000_0000 != 0xC000_0000:
                break  # firmware not running; register is not ours
            seq = (v >> 16) & 0xFF
            if seq == self.last_mcu_seq:
                break
            self.last_mcu_seq = seq
            out.append((v >> 8) & 0xFF)
            self.pending.append((v >> 8) & 0xFF)
            self.received += 1
            self.mcu_ack = seq
            cur = self.r.rd(RX_OFFSET)
            self.r.wr(RX_OFFSET, (cur & 0x00FF_FFFF) | ((seq & 0xFF) << 24))
            if self.verbose:
                print(f"  <- MCU {out[-1]:#04x} (seq {seq})", flush=True)
        return bytes(out)

    def take_pending(self):
        """Everything the MCU has sent since the last call, including bytes
        collected while we were mid-send."""
        self.drain_mcu()
        out = bytes(self.pending)
        self.pending.clear()
        return out

    def mcu_alive(self):
        """Probe with a lone 0x7E. Harmless: it only resets frame state."""
        saved_verbose, self.verbose = self.verbose, False
        try:
            return self.to_mcu(b"\x7e", retries=1) == 1
        finally:
            self.verbose = saved_verbose

    def stats(self):
        return f"sent {self.sent}, received {self.received}, lost {self.lost}"


class Firmware:
    """Starts and watches the test that boots the MCU, locally on this board."""

    def __init__(self, repo, test):
        self.cmd = BOOT_CMD.format(repo=repo, test=test)
        self.proc = None
        self.log = "/tmp/agent_firmware.log"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def boot(self):
        if self.running():
            return False
        logf = open(self.log, "wb")
        self.proc = subprocess.Popen(self.cmd, shell=True, stdout=logf,
                                     stderr=subprocess.STDOUT)
        return True

    def stop(self):
        if self.running():
            subprocess.run("pkill -f cargo-nextest", shell=True)
            self.proc = None
            return True
        return False

    def exit_code(self):
        return None if self.proc is None else self.proc.poll()


def open_serial(port):
    subprocess.run(["stty", "-F", port, "raw", "-echo", "115200"], check=False)
    return os.open(port, os.O_RDWR | os.O_NONBLOCK)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyGS0")
    ap.add_argument("--repo", default="/root/caliptra-mcu-sw")
    ap.add_argument("--test", default=DEFAULT_TEST)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--selftest", action="store_true",
                    help="send one GET_VERSION to the MCU and print the reply")
    ap.add_argument("--boot", action="store_true",
                    help="boot firmware, wait for the MCU, then exit")
    args = ap.parse_args()

    regs = Regs()
    agent = Agent(regs, args.verbose, args.timeout, args.retries)
    fw = Firmware(args.repo, args.test)

    if args.selftest:
        req = bytes([0x7E, 0x7E, 0x7E, 0x7E, 0x01, 0x09, 0x01, 0x10, 0x08, 0xC8,
                     0x05, 0x10, 0x84, 0x00, 0x00, 0x7B, 0x71, 0x7E])
        agent.take_pending()  # discard anything stale
        print(f"sending GET_VERSION ({len(req)} bytes)")
        agent.to_mcu(req)
        reply = bytearray()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            reply += agent.take_pending()
            if len(reply) > 2 and reply[-1] == 0x7E:
                break
            time.sleep(0.01)
        print("reply:", " ".join(f"{b:02X}" for b in reply) or "(nothing)")
        print(agent.stats())
        regs.close()
        return

    if args.boot:
        print("booting firmware...")
        fw.boot()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if agent.mcu_alive():
                print("MCU is up")
                regs.close()
                return
            time.sleep(0.5)
        print("timed out waiting for the MCU; see", fw.log)
        regs.close()
        sys.exit(1)

    if not os.path.exists(args.port):
        sys.exit(f"{args.port} not found - is the USB gadget up?")
    fd = open_serial(args.port)
    print(f"agent ready: {args.port} <-> MCU registers", flush=True)

    control = bytearray()   # control line being assembled (outside frames)
    in_frame = False
    was_alive = False
    booting = False
    next_probe = 0.0

    def reply(text):
        os.write(fd, (text + "\n").encode())
        if args.verbose:
            print(f"  [ctl] {text}", flush=True)

    try:
        while True:
            # ---- serial in: split control lines from frame traffic
            try:
                data = os.read(fd, 256)
            except BlockingIOError:
                data = b""

            if data:
                passthrough = bytearray()
                for b in data:
                    if b == 0x7E:
                        in_frame = not in_frame or True  # any 7E means frame traffic
                        control.clear()
                        passthrough.append(b)
                    elif in_frame:
                        passthrough.append(b)
                    elif b in (0x0A, 0x0D):
                        line = control.decode("ascii", "ignore").strip()
                        control.clear()
                        if line.startswith("!"):
                            cmd = line.upper()
                            if cmd == "!PING":
                                reply("!PONG")
                            elif cmd == "!STATUS":
                                reply("!UP" if agent.mcu_alive() else "!DOWN")
                            elif cmd == "!BOOT":
                                if fw.running():
                                    reply("!BUSY")
                                elif fw.boot():
                                    booting = True
                                    reply("!BOOTING")
                            elif cmd == "!STOP":
                                reply("!STOPPED" if fw.stop() else "!IDLE")
                            else:
                                reply("!UNKNOWN")
                    elif 32 <= b < 127:
                        if len(control) < 64:
                            control.append(b)
                if passthrough:
                    agent.to_mcu(bytes(passthrough))
                    in_frame = False

            # ---- MCU out (including anything collected while sending)
            out = agent.take_pending()
            if out:
                os.write(fd, out)

            # ---- liveness, reported as state changes
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + 1.0
                alive = agent.mcu_alive()
                if alive and not was_alive:
                    reply("!READY")
                    booting = False
                elif was_alive and not alive:
                    reply("!DOWN")
                was_alive = alive
                if booting and not fw.running():
                    code = fw.exit_code()
                    reply(f"!EXIT {code}")
                    booting = False

            if not data and not out:
                time.sleep(0.002)
    except KeyboardInterrupt:
        print(f"\nstopping: {agent.stats()}")
    finally:
        os.close(fd)
        regs.close()


if __name__ == "__main__":
    main()
