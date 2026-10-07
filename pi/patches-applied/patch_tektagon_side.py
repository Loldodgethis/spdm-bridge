#!/usr/bin/env python3
"""
patch_tektagon_side.py - build out the Tektagon half: Pi as responder, plus
an I2C slave transport for when the Tektagon drives the bus.

Run on the Pi, next to the spdm_bridge/ directory:
    python3 patch_tektagon_side.py

Two additions, neither of which needs the Tektagon to be working yet.

1. RESPONDER MODE for the Tektagon endpoint. Madhan's design has the Pi as
   responder to the Tektagon and requestor to the FPGA. Two ways to answer:

       --mode proxy       (default) forward the request to the FPGA and send
                          the MCU's real answer back - true end-to-end
       --mode local       answer from the Pi itself, without the FPGA
       --mode observe     log only, answer nothing

   Proxy mode is the real target: Tektagon asks, Caliptra answers, the Pi
   sits in the middle and shows both sides.

2. BSC SLAVE TRANSPORT (spec "bsc:0x38"). The Pi's normal I2C controller is
   master-only, so if the Tektagon drives the bus, the Pi can only be heard
   on the BSC peripheral:

       GPIO 18 (pin 12) = SDA
       GPIO 19 (pin 35) = SCL      plus a shared ground

   Needs pigpio running (sudo pigpiod). Once the lines move to those pins:

       python3 -m spdm_bridge --fpga serial:/dev/ttyACM0 --tektagon bsc:0x38

Restart the framework afterwards.
"""
import sys

# --------------------------------------------------------------- transports
T = "spdm_bridge/transports.py"
try:
    s = open(T).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "BscSlaveTransport" in s:
    sys.exit("already patched")

BSC = '''# --------------------------------------------------------------- i2c slave
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


'''

anchor = "# ------------------------------------------------------------------ factory"
assert s.count(anchor) == 1, "factory anchor not unique"
s = s.replace(anchor, BSC + anchor)

old = """    if kind == "i2c":"""
new = """    if kind == "bsc":
        addr = int(args[0], 0) if args else 0x38
        return BscSlaveTransport(addr, name=name)
    if kind == "i2c":"""
assert s.count(old) == 1, "factory branch anchor not unique"
s = s.replace(old, new)

old = '''        serial:/dev/ttyUSB1:115200
        ssh:root@172.31.1.116'''
new = '''        serial:/dev/ttyUSB1:115200
        ssh:root@172.31.1.116
        bsc:0x38          (Pi as I2C slave on GPIO 18/19)'''
if s.count(old) == 1:
    s = s.replace(old, new)

open(T, "w").write(s)
print("patched", T)

# ------------------------------------------------------------------- bridge
B = "spdm_bridge/bridge.py"
s = open(B).read()

if "responder_mode" not in s:
    old = """    def __init__(self, fpga: Transport, tektagon: Transport, *,
                 relay: bool = True, poll_interval: float = 0.02,
                 log_limit: int = 2000) -> None:"""
    new = """    def __init__(self, fpga: Transport, tektagon: Transport, *,
                 relay: bool = True, poll_interval: float = 0.02,
                 log_limit: int = 2000, responder_mode: str = "proxy") -> None:"""
    assert s.count(old) == 1, "init anchor not unique"
    s = s.replace(old, new)

    old = """        self.relay = relay"""
    new = """        self.relay = relay
        # how the Pi answers requests arriving from the Tektagon:
        # proxy = forward to the FPGA, local = answer here, observe = log only
        self.responder_mode = responder_mode"""
    assert s.count(old) == 1, "relay anchor not unique"
    s = s.replace(old, new)

    old = """    def _handle(self, source: str, msg: protocol.Message) -> None:
        target = self.other(source)
        relayed = False
        if self.relay and not msg.error:"""
    new = '''    def _handle(self, source: str, msg: protocol.Message) -> None:
        target = self.other(source)
        relayed = False

        # A request arriving from the Tektagon: the Pi is the responder here.
        if (source == "tektagon" and not msg.error
                and msg.kind == "request"):
            if self.responder_mode == "local":
                reply = protocol.response_to(msg)
                if reply:
                    self.endpoints["tektagon"].send(reply)
                    self.note(f"answered {msg.name} locally", kind="info")
                else:
                    self.note(f"no local answer for {msg.name}", kind="error")
            elif self.responder_mode == "observe":
                self.note(f"observed {msg.name} (not answering)", kind="info")
            # proxy mode falls through to the relay below: the request goes
            # to the FPGA and the MCU's real answer comes back to the Tektagon

        if self.relay and not msg.error:'''
    assert s.count(old) == 1, "handle anchor not unique"
    s = s.replace(old, new)

    old = """            "relay": self.relay,"""
    new = """            "relay": self.relay,
            "responder_mode": self.responder_mode,"""
    assert s.count(old) == 1, "status anchor not unique"
    s = s.replace(old, new)

    open(B, "w").write(s)
    print("patched", B)

# ----------------------------------------------------------------- __main__
M = "spdm_bridge/__main__.py"
s = open(M).read()
if "--mode" not in s:
    old = """    ap.add_argument("--raw-log","""
    new = """    ap.add_argument("--mode", choices=["proxy", "local", "observe"],
                    default="proxy",
                    help="how the Pi answers Tektagon requests: forward them to "
                         "the FPGA (proxy), answer here (local), or just log "
                         "them (observe)")
    ap.add_argument("--raw-log","""
    assert s.count(old) == 1, "arg anchor not unique"
    s = s.replace(old, new)

    old = """    bridge = Bridge(fpga, tek, relay=not args.no_relay)"""
    new = """    bridge = Bridge(fpga, tek, relay=not args.no_relay,
                    responder_mode=args.mode)"""
    assert s.count(old) == 1, "bridge anchor not unique"
    s = s.replace(old, new)

    old = '''    bridge.note(f"bridge up: fpga={args.fpga} tektagon={args.tektagon} "
                f"relay={not args.no_relay}")'''
    new = '''    bridge.note(f"bridge up: fpga={args.fpga} tektagon={args.tektagon} "
                f"relay={not args.no_relay} responder={args.mode}")'''
    assert s.count(old) == 1, "note anchor not unique"
    s = s.replace(old, new)

    open(M, "w").write(s)
    print("patched", M)

print("restart the framework to pick this up")
