#!/usr/bin/env python3
"""
vck190_agent.py - bridge USB gadget serial to the MCU, and own the firmware run.

Runs ON THE VCK190 as root, as the vck190-agent systemd service.

    Pi  ->  /dev/ttyACM0  ==USB==  /dev/ttyGS0  ->  this agent
                                                        |
                                           FPGA wrapper registers
                                                        |
                                                  Caliptra MCU

Two jobs:

1. Byte pump. Complete SPDM frames from the Pi go to the MCU; MCU bytes come
   back. The agent itself NEVER puts traffic on the register channel - there
   is no liveness probe. One writer only.

2. Firmware owner. The MCU only exists while a nextest run is going. The
   agent starts that run itself (the responder conformance test, which keeps
   the firmware alive ~30 min) and decides readiness by watching the firmware
   log for the spdm-lib run-loop line, not by poking the MCU.

Control lines sit OUTSIDE SPDM frames and start with '!':

    !BOOT     start firmware. If our run is already up: reply !READY (or
              !BOOTING if still coming up) and do NOT restart it. Any other
              firmware run (e.g. a manual nextest) is killed first.
    !STOP     kill every firmware run on the board
    !STATUS   !UP (SPDM stack listening) / !BOOTING / !DOWN, answered instantly
    !PING     !PONG

Unsolicited: !READY when the run-loop line appears, !DOWN when the MCU
reboots or the run ends, !EXIT <code> when the nextest process exits.

While our firmware is booting but not ready, SPDM frames from the Pi are
dropped instead of being pushed into an MCU that is not listening (that used
to block this loop for seconds per byte).

Registers:
    0xa401012c  host -> MCU   [7:0] byte [8] valid [23:16] seq [31:24] ack
    0xa4010130  MCU -> host   [7:0] ack  [15:8] byte [23:16] seq

Usage:
    python3 vck190_agent.py                 # serve on /dev/ttyGS0
    python3 vck190_agent.py --verbose
    python3 vck190_agent.py --boot          # boot firmware, wait for SPDM, exit
    python3 vck190_agent.py --selftest      # one GET_VERSION; stop the service
                                            # first or you have two writers
"""
import argparse
import mmap
import os
import signal
import struct
import subprocess
import sys
import time

UIO = "/dev/uio0"
MAP = 0
RX_OFFSET = 0x12C
RET_OFFSET = 0x130
VALID = 1 << 8

DEFAULT_TEST = ("test_mctp_spdm_responder_conformance::test::"
                "test_mctp_spdm_responder_conformance")
DEFAULT_PROFILE = "nightly-ci-spdm"
FW_LOG = "/tmp/agent_firmware.log"
READY_MARKER = "SPDM_REG: starting"
REBOOT_MARKER = "FPGA MCU ROM"

BOOT_CMD = (
    "cd {repo} && SPDM_VALIDATOR_DIR={validator} "
    "CPTRA_FIRMWARE_BUNDLE={home}/all-fw.zip cargo-nextest nextest run "
    "--workspace-remap=. --archive-file {home}/caliptra-test-binaries.tar.zst "
    "--profile={profile} --run-ignored all --no-capture "
    "-E 'test(={test})'"
)

# Anything matching these is a firmware run (ours or a manual one).
KILL_PATTERNS = ("cargo-nextest", "nextest-archive",
                 "spdm_device_validator", "spdm_requester_emu")


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


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
    """Register-channel byte pump. Never originates traffic."""

    def __init__(self, regs, verbose=False, timeout=1.0, retries=4):
        self.r = regs
        self.verbose = verbose
        self.timeout = timeout
        self.retries = max(1, retries)
        self.sent = self.received = self.lost = 0
        self.pending = bytearray()   # bytes drained while we were mid-send
        self.resync()

    def resync(self):
        """Adopt whatever sequence numbers the registers hold now. Called at
        start and whenever a fresh firmware instance becomes ready."""
        self.tx_seq = (self.r.rd(RX_OFFSET) >> 16) & 0xFF
        self.last_mcu_seq = (self.r.rd(RET_OFFSET) >> 16) & 0xFF
        self.mcu_ack = self.last_mcu_seq
        self.pending.clear()

    # ---------------------------------------------------------- host -> MCU
    def _rx_word(self, byte, seq):
        return ((self.mcu_ack & 0xFF) << 24) | ((seq & 0xFF) << 16) | VALID | byte

    def to_mcu(self, data, retries=None, stop_on_loss=False):
        """Write bytes to the MCU; each byte is re-presented until taken.
        With stop_on_loss, give up on the rest of `data` after the first
        unacknowledged byte so a dead MCU cannot stall the caller."""
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
                if stop_on_loss:
                    break
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
        self.drain_mcu()
        out = bytes(self.pending)
        self.pending.clear()
        return out

    def stats(self):
        return f"sent {self.sent}, received {self.received}, lost {self.lost}"


class Firmware:
    """Starts the firmware run and tracks readiness from its log."""

    def __init__(self, cmd, logpath=FW_LOG, ready_marker=READY_MARKER):
        self.cmd = cmd
        self.log = logpath
        self.ready_marker = ready_marker.encode()
        self.reboot_marker = REBOOT_MARKER.encode()
        self.proc = None
        self.ready = False
        self.exit_reported = True
        self._pos = 0
        self._tail = b""

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def kill_all(self):
        """Stop our run and any other firmware run on the board."""
        if self.running():
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for pat in KILL_PATTERNS:
            subprocess.run(["pkill", "-f", pat], check=False)
        time.sleep(1.0)
        for pat in KILL_PATTERNS:
            subprocess.run(["pkill", "-9", "-f", pat], check=False)
        self.proc = None
        self.ready = False
        self.exit_reported = True

    def boot(self):
        self.kill_all()
        logf = open(self.log, "wb")
        self.proc = subprocess.Popen(["/bin/sh", "-c", self.cmd],
                                     stdout=logf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL,
                                     start_new_session=True)
        logf.close()
        self.ready = False
        self.exit_reported = False
        self._pos = 0
        self._tail = b""
        log(f"firmware: booting (pid {self.proc.pid}), log {self.log}")

    def poll(self):
        """Read new log output and return a list of events:
        'ready', 'down', ('exit', code)."""
        events = []
        if self.proc is None:
            return events
        try:
            with open(self.log, "rb") as f:
                f.seek(self._pos)
                chunk = f.read()
                self._pos = f.tell()
        except OSError:
            chunk = b""
        if chunk:
            buf = self._tail + chunk
            self._tail = buf[-256:]
            r = buf.rfind(self.ready_marker)
            b = buf.rfind(self.reboot_marker)
            if r > b and not self.ready:
                self.ready = True
                events.append("ready")
            elif b > r and self.ready:
                self.ready = False
                events.append("down")
        if not self.exit_reported and self.proc.poll() is not None:
            self.exit_reported = True
            if self.ready:
                self.ready = False
                events.append("down")
            events.append(("exit", self.proc.returncode))
        return events


def open_serial(port):
    subprocess.run(["stty", "-F", port, "raw", "-echo", "115200"], check=False)
    return os.open(port, os.O_RDWR | os.O_NONBLOCK)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyGS0")
    ap.add_argument("--repo", default="/root/caliptra-mcu-sw")
    ap.add_argument("--home", default="/root",
                    help="where all-fw.zip and caliptra-test-binaries.tar.zst live")
    ap.add_argument("--test", default=DEFAULT_TEST)
    ap.add_argument("--profile", default=DEFAULT_PROFILE)
    ap.add_argument("--validator", default="/root/spdm-emu/build/bin",
                    help="SPDM_VALIDATOR_DIR for the boot command")
    ap.add_argument("--ready-marker", default=READY_MARKER)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--timeout", type=float, default=1.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--selftest", action="store_true",
                    help="send one GET_VERSION to the MCU and print the reply")
    ap.add_argument("--boot", action="store_true",
                    help="boot firmware, wait for the SPDM stack, then exit")
    args = ap.parse_args()

    cmd = BOOT_CMD.format(repo=args.repo, validator=args.validator,
                          home=args.home, profile=args.profile, test=args.test)
    regs = Regs()
    agent = Agent(regs, args.verbose, args.timeout, args.retries)
    fw = Firmware(cmd, ready_marker=args.ready_marker)

    if args.selftest:
        req = bytes([0x7E, 0x05, 0x10, 0x84, 0x00, 0x00, 0x45, 0x0F, 0x7E])
        agent.take_pending()
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
        fw.boot()
        deadline = time.monotonic() + 1500
        while time.monotonic() < deadline:
            for ev in fw.poll():
                if ev == "ready":
                    log("SPDM stack is up; firmware keeps running in the background")
                    regs.close()
                    return
                if isinstance(ev, tuple):
                    log(f"firmware run exited with {ev[1]} before the SPDM stack came up")
                    regs.close()
                    sys.exit(1)
            time.sleep(0.5)
        log(f"timed out waiting for the SPDM stack; see {fw.log}")
        regs.close()
        sys.exit(1)

    if not os.path.exists(args.port):
        sys.exit(f"{args.port} not found - is the USB gadget up?")
    fd = open_serial(args.port)
    log(f"agent ready: {args.port} <-> MCU registers (no probing)")

    control = bytearray()
    frame = bytearray()
    in_frame = False
    last_rx = 0.0
    next_poll = 0.0

    def reply(text):
        os.write(fd, (text + "\n").encode())
        log(f"[ctl] {text}")

    def handle_frame(data):
        if fw.running() and not fw.ready:
            log(f"dropped {len(data)}-byte frame: firmware still booting")
            return
        sent = agent.to_mcu(data, stop_on_loss=True)
        if sent != len(data):
            log(f"frame: only {sent}/{len(data)} bytes acked by the MCU")

    def handle_control(line):
        cmd = line.upper()
        if cmd == "!PING":
            reply("!PONG")
        elif cmd == "!STATUS":
            if fw.ready:
                reply("!UP")
            elif fw.running():
                reply("!BOOTING")
            else:
                reply("!DOWN")
        elif cmd == "!BOOT":
            if fw.running():
                reply("!READY" if fw.ready else "!BOOTING")
            else:
                fw.boot()
                reply("!BOOTING")
        elif cmd == "!STOP":
            fw.kill_all()
            log("firmware: stopped")
            reply("!STOPPED")
        else:
            reply("!UNKNOWN")

    try:
        while True:
            now = time.monotonic()

            # ---- serial in: complete frames go to the MCU, lines are control
            try:
                data = os.read(fd, 512)
            except BlockingIOError:
                data = b""

            if in_frame and data == b"" and now - last_rx > 2.0:
                log(f"discarded unterminated {len(frame)}-byte frame")
                frame = bytearray()
                in_frame = False

            for b in data:
                last_rx = now
                if b == 0x7E:
                    if not in_frame:
                        in_frame = True
                        frame = bytearray([b])
                        control.clear()
                    elif any(x != 0x7E for x in frame):
                        frame.append(b)
                        handle_frame(bytes(frame))
                        frame = bytearray()
                        in_frame = False
                    else:
                        frame.append(b)      # repeated opening delimiter
                elif in_frame:
                    frame.append(b)
                    if len(frame) > 20000:
                        log("discarded oversized frame")
                        frame = bytearray()
                        in_frame = False
                elif b in (0x0A, 0x0D):
                    line = control.decode("ascii", "ignore").strip()
                    control.clear()
                    if line.startswith("!"):
                        handle_control(line)
                elif 32 <= b < 127 and len(control) < 64:
                    control.append(b)

            # ---- MCU out
            out = agent.take_pending()
            if out:
                os.write(fd, out)

            # ---- firmware state from its log
            if now >= next_poll:
                next_poll = now + 0.5
                for ev in fw.poll():
                    if ev == "ready":
                        agent.resync()
                        log("firmware: SPDM stack listening")
                        reply("!READY")
                    elif ev == "down":
                        log("firmware: MCU went down / rebooted")
                        reply("!DOWN")
                    else:
                        log(f"firmware: run exited with {ev[1]}")
                        reply(f"!EXIT {ev[1]}")

            if not data and not out:
                time.sleep(0.002)
    except KeyboardInterrupt:
        log(f"stopping: {agent.stats()}")
    finally:
        os.close(fd)
        regs.close()


if __name__ == "__main__":
    main()
