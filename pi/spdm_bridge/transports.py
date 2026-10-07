"""
transports.py - one class per way of reaching a board. All swappable.

Every transport is the same shape:

    open()            connect
    send(data)        put bytes on the wire
    poll()            return whatever bytes arrived since last call (may be b"")
    close()           disconnect

Nothing here blocks for long; the bridge polls in a loop. Optional dependencies
(pyserial, smbus2) are imported only when that transport is actually used, so
this runs on a laptop with neither installed.

Available:
    MockTransport       responds to SPDM requests locally, for GUI work
    SerialTransport     USB serial (Pi <-> VCK190 console, or any UART)
    SshUartTxTransport  drives uart_tx.py on the VCK190 over SSH
    I2CTransport        Pi as I2C master to the Tektagon
"""
from __future__ import annotations

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from typing import Optional

from . import protocol


class Transport:
    """Base class. Subclasses override open/send/poll/close."""

    name = "transport"
    #: set False on transports that cannot read back (write-only paths)
    can_receive = True

    def open(self) -> None:
        pass

    def send(self, data: bytes) -> None:
        raise NotImplementedError

    def poll(self) -> bytes:
        return b""

    def close(self) -> None:
        pass

    def status(self) -> str:
        return "ok"


# --------------------------------------------------------------------- mock
class MockTransport(Transport):
    """A fake board. Decodes what it is sent and answers like a responder.

    Lets the whole framework and GUI run with no hardware attached, and gives
    a known-good reference when a real link misbehaves.
    """

    def __init__(self, name: str = "mock", delay: float = 0.4,
                 respond: bool = True) -> None:
        self.name = name
        self.delay = delay
        self.respond = respond
        self._rx: "queue.Queue[bytes]" = queue.Queue()
        self._decoder = protocol.FrameDecoder()
        self._opened = False

    def open(self) -> None:
        self._opened = True

    def send(self, data: bytes) -> None:
        for msg in self._decoder.feed(data):
            if not self.respond:
                continue
            reply = protocol.response_to(msg)
            if reply:
                threading.Timer(self.delay, self._rx.put, args=(reply,)).start()

    def poll(self) -> bytes:
        out = bytearray()
        while True:
            try:
                out += self._rx.get_nowait()
            except queue.Empty:
                break
        return bytes(out)

    def inject(self, data: bytes) -> None:
        """Pretend the board sent us these bytes (for tests and demos)."""
        self._rx.put(data)

    def status(self) -> str:
        return "mock (no hardware)" if self._opened else "closed"


# ------------------------------------------------------------------- serial
class SerialTransport(Transport):
    """Plain USB serial. Use for any real UART link, once one exists."""

    can_receive = True

    def __init__(self, port: str, baud: int = 115200, name: str = "serial") -> None:
        self.name = name
        self.port = port
        self.baud = baud
        self._ser = None

    def open(self) -> None:
        import serial  # pip install pyserial

        self._ser = serial.Serial(self.port, self.baud, timeout=0)

    def send(self, data: bytes) -> None:
        if self._ser:
            self._ser.write(data)

    def poll(self) -> bytes:
        if not self._ser:
            return b""
        n = self._ser.in_waiting
        return self._ser.read(n) if n else b""

    def close(self) -> None:
        if self._ser:
            self._ser.close()
            self._ser = None

    def status(self) -> str:
        return f"{self.port} @ {self.baud}" if self._ser else "closed"


# ---------------------------------------------------------------- ssh + uio
class SshUartTxTransport(Transport):
    """Reach the MCU by running uart_tx.py on the VCK190 over SSH.

    This is the path that works today: bytes go into the FPGA wrapper register
    and the MCU's poll loop picks them up. It is WRITE ONLY - the MCU's replies
    come out of the debug FIFO into the test log, not back to us - so poll()
    always returns nothing until a return path exists.
    """

    can_receive = False

    def __init__(self, host: str, script: str = "/root/uart_tx.py",
                 name: str = "fpga-ssh", ssh_opts: str = "-n -o BatchMode=yes") -> None:
        self.name = name
        self.host = host
        self.script = script
        self.ssh_opts = ssh_opts
        self._last_error: Optional[str] = None

    def open(self) -> None:
        if not shutil.which("ssh"):
            raise RuntimeError("ssh not found on this machine")

    def send(self, data: bytes) -> None:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(data)
            local = f.name
        remote = f"/tmp/bridge_{os.path.basename(local)}"
        try:
            subprocess.run(["scp", "-q", local, f"{self.host}:{remote}"],
                           check=True, timeout=30)
            subprocess.run(
                ["ssh", *self.ssh_opts.split(), self.host,
                 f"python3 {self.script} --file {remote}; rm -f {remote}"],
                check=True, timeout=180, capture_output=True)
            self._last_error = None
        except subprocess.SubprocessError as exc:
            self._last_error = str(exc)
        finally:
            os.unlink(local)

    def status(self) -> str:
        return self._last_error or f"{self.host} (write-only)"


# ---------------------------------------------------------------------- i2c
class I2CTransport(Transport):
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


# --------------------------------------------------------------- i2c slave
class BscSlaveTransport(Transport):
    """Pi as an I2C SLAVE, using the BSC peripheral via pigpio.

    For when the other board is bus master. The Pi's main controller
    (i2c-bcm2835 on GPIO 2/3) cannot do this at all; the BSC block can, but
    it lives on different pins:

        GPIO 18 (pin 12) = SDA
        GPIO 19 (pin 35) = SCL      plus a shared ground

    Start the daemon first:  sudo pigpiod
    """

    def __init__(self, address: int = 0x38, name: str = "tektagon-bsc") -> None:
        self.name = name
        self.address = address
        self._pi = None
        self._tx = bytearray()      # queued for the master's next read
        self._rx_bytes = 0
        self._last_error = None

    def open(self) -> None:
        import pigpio  # sudo apt install python3-pigpio

        self._pi = pigpio.pi()
        if not self._pi.connected:
            raise RuntimeError("pigpio daemon not running - try: sudo pigpiod")
        self._pi.bsc_i2c(self.address)   # open and clear

    def send(self, data: bytes) -> None:
        """Queue bytes for the master to collect on its next read."""
        self._tx += data

    def poll(self) -> bytes:
        if not self._pi:
            return b""
        try:
            if self._tx:
                status, count, data = self._pi.bsc_i2c(self.address, bytes(self._tx))
                self._tx.clear()
            else:
                status, count, data = self._pi.bsc_i2c(self.address)
        except Exception as exc:
            self._last_error = str(exc)
            return b""
        if count:
            self._rx_bytes += count
            return bytes(data)
        return b""

    def close(self) -> None:
        if self._pi:
            try:
                self._pi.bsc_i2c(0)
            finally:
                self._pi.stop()
                self._pi = None

    def status(self) -> str:
        if self._last_error:
            return self._last_error
        if not self._pi:
            return "closed"
        return (f"slave {self.address:#04x} on GPIO18/19 - "
                f"{self._rx_bytes}B received")


# ------------------------------------------------------------------ factory
def build(spec: str, name: str) -> Transport:
    """Build a transport from a CLI string.

    Examples:
        mock
        mock:noreply
        serial:/dev/ttyUSB1
        serial:/dev/ttyUSB1:115200
        ssh:root@172.31.1.116
        bsc:0x38          (Pi as I2C slave on GPIO 18/19)
        i2c:0x38
        i2c:0x38:1
    """
    kind, _, rest = spec.partition(":")
    args = rest.split(":") if rest else []

    if kind == "mock":
        return MockTransport(name=name, respond="noreply" not in args)
    if kind == "serial":
        port = args[0] if args else "/dev/ttyUSB0"
        baud = int(args[1]) if len(args) > 1 else 115200
        return SerialTransport(port, baud, name=name)
    if kind == "ssh":
        host = args[0] if args else "root@172.31.1.116"
        return SshUartTxTransport(host, name=name)
    if kind == "bsc":
        addr = int(args[0], 0) if args else 0x38
        return BscSlaveTransport(addr, name=name)
    if kind == "i2c":
        addr = int(args[0], 0) if args else 0x38
        bus = int(args[1]) if len(args) > 1 else 1
        return I2CTransport(addr, bus, name=name)
    raise ValueError(f"unknown transport {spec!r}")
