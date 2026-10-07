#!/usr/bin/env python3
"""
patch_i2c_quiet.py - make the I2C transport self-tuning and quiet.

Run on the Pi, next to the spdm_bridge/ directory:
    python3 patch_i2c_quiet.py

The old transport assumed one register layout I made up, so a device that
does not use it NAKs every poll and fills the GUI with
"[Errno 121] Remote I/O error".

This replaces it with a transport that:

  * tries several read patterns (raw read, SMBus block read at the MCTP
    command code 0x0F, length-then-data registers) and sticks with whichever
    one actually returns data
  * backs off when reads fail - 50ms, then doubling to 5s - instead of
    hammering the bus
  * reports state in the status pill ("no reply yet", "reading via raw read")
    rather than repeating the same error
  * writes using the MCTP-over-I2C block form (cmd 0x0F + byte count), which
    is what the DSP0237 binding specifies, with a raw-write fallback

Restart the framework afterwards.
"""
import sys

T = "spdm_bridge/transports.py"
try:
    s = open(T).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "_read_patterns" in s:
    sys.exit("already patched")

start = s.index("class I2CTransport(Transport):")
end = s.index("# ------------------------------------------------------------------ factory")

NEW = '''class I2CTransport(Transport):
    """Pi as I2C master, talking to the Tektagon.

    The device's access pattern is not known up front, so this tries a few
    and keeps whichever works. Failures back off quietly rather than filling
    the log, because a device that never answers would otherwise error on
    every poll.
    """

    MCTP_CMD = 0x0F          # SMBus command code used by MCTP over I2C
    CHUNK = 32
    MIN_BACKOFF = 0.05
    MAX_BACKOFF = 5.0

    def __init__(self, address: int = 0x38, bus: int = 1,
                 name: str = "tektagon-i2c", poll_interval: float = 0.05) -> None:
        self.name = name
        self.address = address
        self.bus_no = bus
        self.poll_interval = poll_interval
        self._bus = None
        self._next_poll = 0.0
        self._backoff = self.MIN_BACKOFF
        self._pattern = None      # whichever read pattern worked
        self._reads = 0
        self._fails = 0
        self._last_error = None
        self._wrote = 0

    def open(self) -> None:
        from smbus2 import SMBus  # pip install smbus2

        self._bus = SMBus(self.bus_no)

    # ------------------------------------------------------------- reading
    def _read_patterns(self):
        """Each returns bytes, or raises OSError if the device refuses."""
        from smbus2 import i2c_msg

        def raw(n=self.CHUNK):
            msg = i2c_msg.read(self.address, n)
            self._bus.i2c_rdwr(msg)
            return bytes(msg)

        def block():
            return bytes(self._bus.read_block_data(self.address, self.MCTP_CMD))

        def len_then_data():
            n = self._bus.read_byte_data(self.address, 0x02)
            if not n:
                return b""
            msg = i2c_msg.read(self.address, min(n, self.CHUNK))
            self._bus.i2c_rdwr(i2c_msg.write(self.address, bytes([0x01])), msg)
            return bytes(msg)

        return [("raw read", raw), ("block read 0x0F", block),
                ("len+data regs", len_then_data)]

    def poll(self) -> bytes:
        if not self._bus or time.monotonic() < self._next_poll:
            return b""

        patterns = self._read_patterns()
        if self._pattern is not None:
            patterns = [p for p in patterns if p[0] == self._pattern]

        for label, fn in patterns:
            try:
                data = fn()
            except OSError as exc:
                self._last_error = f"{label}: {exc}"
                continue
            if data and any(data):          # all-zero reads are not traffic
                self._pattern = label
                self._reads += 1
                self._fails = 0
                self._backoff = self.MIN_BACKOFF
                self._next_poll = time.monotonic() + self.poll_interval
                return data

        # nothing useful: back off so a silent device does not flood the log
        self._fails += 1
        self._backoff = min(self._backoff * 2, self.MAX_BACKOFF)
        self._next_poll = time.monotonic() + self._backoff
        return b""

    # ------------------------------------------------------------- writing
    def send(self, data: bytes) -> None:
        from smbus2 import i2c_msg

        if not self._bus:
            return
        for i in range(0, len(data), self.CHUNK):
            chunk = data[i:i + self.CHUNK]
            body = bytes([self.MCTP_CMD, len(chunk)]) + chunk
            try:
                self._bus.i2c_rdwr(i2c_msg.write(self.address, body))
                self._wrote += len(chunk)
                self._last_error = None
            except OSError:
                try:  # some devices want the bytes with no command code
                    self._bus.i2c_rdwr(i2c_msg.write(self.address, chunk))
                    self._wrote += len(chunk)
                    self._last_error = None
                except OSError as exc:
                    self._last_error = f"write: {exc}"
                    return

    def close(self) -> None:
        if self._bus:
            self._bus.close()
            self._bus = None

    def status(self) -> str:
        if not self._bus:
            return "closed"
        where = f"bus {self.bus_no} addr {self.address:#04x}"
        if self._pattern:
            return f"{where} - reading via {self._pattern} ({self._reads} reads)"
        if self._wrote:
            return f"{where} - wrote {self._wrote}B, no reply yet"
        return f"{where} - no reply yet"


'''

s = s[:start] + NEW + s[end:]
open(T, "w").write(s)
print("patched", T)
print("restart the framework to pick this up")
