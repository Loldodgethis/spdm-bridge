"""
bridge.py - the middle of the framework.

Holds two endpoints (by convention "fpga" and "tektagon"), polls both, decodes
every frame, records it in a shared log, and optionally relays it to the other
side. The GUI subscribes to the same log.

Direction naming in the log:
    fpga -> tektagon     something the FPGA side put on the wire
    tektagon -> fpga     something the Tektagon side put on the wire
    host -> fpga         injected by the operator from the GUI
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from . import protocol
from .transports import Transport


@dataclass
class LogEntry:
    seq: int
    t: float
    source: str
    target: str
    direction: str
    summary: str
    kind: str
    hexdump: str
    name: str
    relayed: bool = False

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["time"] = time.strftime("%H:%M:%S", time.localtime(self.t))
        return d


class Bridge:
    def __init__(self, fpga: Transport, tektagon: Transport, *,
                 relay: bool = True, poll_interval: float = 0.02,
                 log_limit: int = 2000, responder_mode: str = "proxy") -> None:
        self.endpoints: Dict[str, Transport] = {"fpga": fpga, "tektagon": tektagon}
        self.relay = relay
        # how the Pi answers requests arriving from the Tektagon:
        # proxy = forward to the FPGA, local = answer here, observe = log only
        self.responder_mode = responder_mode
        self.poll_interval = poll_interval
        self.log_limit = log_limit

        self.log: List[LogEntry] = []
        self.counters = {"fpga": 0, "tektagon": 0, "host": 0, "errors": 0}
        self._decoders = {k: protocol.FrameDecoder() for k in self.endpoints}
        self._control = {}
        # decoded responses, by log sequence number, for wait_response()
        self._responses = {}
        self.last_control = {}
        # local buffer of raw traffic, so the UART window can show history
        # SPDM version agreed with the responder; set from its VERSION
        # response. 0x10 only until we have been told otherwise.
        self.negotiated_version = 0x10
        self.raw_log = []
        self.raw_limit = 8000
        self.raw_file = None
        self._raw_seq = itertools.count(1)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[LogEntry], None]] = []
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self.started_at: Optional[float] = None

    # ------------------------------------------------------------- plumbing
    def subscribe(self, fn: Callable[[LogEntry], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def _emit(self, entry: LogEntry) -> None:
        with self._lock:
            self.log.append(entry)
            if len(self.log) > self.log_limit:
                del self.log[: len(self.log) // 4]
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(entry)
            except Exception:
                pass

    def other(self, name: str) -> str:
        return "tektagon" if name == "fpga" else "fpga"

    # ---------------------------------------------------------------- start
    def start(self) -> None:
        self.started_at = time.time()
        for name, ep in self.endpoints.items():
            try:
                ep.open()
            except Exception as exc:
                self.note(f"{name}: open failed: {exc}", kind="error")
                continue
            if ep.can_receive:
                t = threading.Thread(target=self._pump, args=(name,), daemon=True)
                t.start()
                self._threads.append(t)
            else:
                self.note(f"{name}: write-only transport, not polling for replies")

    def stop(self) -> None:
        self._stop.set()
        for ep in self.endpoints.values():
            try:
                ep.close()
            except Exception:
                pass

    def _pump(self, name: str) -> None:
        ep = self.endpoints[name]
        decoder = self._decoders[name]
        while not self._stop.is_set():
            try:
                data = ep.poll()
            except Exception as exc:
                self.note(f"{name}: poll failed: {exc}", kind="error")
                time.sleep(0.5)
                continue
            if data:
                self.record_raw(name, "in", data)
                for line in self._control_lines(name, data):
                    self.note(f"{name}: {line}", kind="info")
                for msg in decoder.feed(data):
                    self._handle(name, msg)
            time.sleep(self.poll_interval)

    # -------------------------------------------------------------- traffic
    def _handle(self, source: str, msg: protocol.Message) -> None:
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

        if self.relay and not msg.error:
            try:
                self.endpoints[target].send(msg.raw_frame())
                relayed = True
            except Exception as exc:
                self.note(f"relay to {target} failed: {exc}", kind="error")

        # Learn the version from a VERSION response so later requests match.
        if not msg.error and msg.spdm_code == 0x04:
            offered = protocol.parse_version_response(msg)
            if offered:
                self.negotiated_version = offered[0]
                self.note(
                    "responder offers SPDM "
                    + ", ".join(f"1.{v & 0xF}" for v in offered)
                    + f" - using 1.{offered[0] & 0xF}",
                    kind="info")

        self.counters[source] = self.counters.get(source, 0) + 1
        if msg.error:
            self.counters["errors"] += 1

        entry = LogEntry(
            seq=next(self._seq), t=time.time(), source=source, target=target,
            direction=f"{source} -> {target}", summary=msg.summary(),
            kind=msg.kind, hexdump=msg.hex(), name=msg.name, relayed=relayed,
        )
        with self._lock:
            self._responses[entry.seq] = msg
        self._emit(entry)

    def record_raw(self, endpoint: str, direction: str, data: bytes) -> None:
        """Buffer a chunk of raw bytes exactly as it crossed the wire."""
        if not data:
            return
        entry = {
            "seq": next(self._raw_seq),
            "t": time.time(),
            "time": time.strftime("%H:%M:%S", time.localtime()),
            "endpoint": endpoint,
            "direction": direction,          # "in" (from board) or "out" (to board)
            "n": len(data),
            "hex": " ".join(f"{b:02X}" for b in data),
            "text": "".join(chr(b) if 32 <= b < 127 else "." for b in data),
        }
        with self._lock:
            self.raw_log.append(entry)
            if len(self.raw_log) > self.raw_limit:
                del self.raw_log[: len(self.raw_log) // 4]
        if self.raw_file:
            try:
                arrow = "<-" if direction == "in" else "->"
                self.raw_file.write(
                    f"{entry['time']} {endpoint} {arrow} {entry['hex']}\n")
                self.raw_file.flush()
            except Exception:
                pass

    def raw_snapshot(self, since: int = 0):
        with self._lock:
            return [e for e in self.raw_log if e["seq"] > since]

    def _control_lines(self, name: str, data: bytes):
        """Pull '!...' status lines (from the agent) out of the byte stream.

        They travel outside SPDM frames, so the frame decoder ignores them;
        this surfaces them in the log instead of dropping them silently.
        """
        buf = self._control.setdefault(name, bytearray())
        lines = []
        for b in data:
            if b in (0x0A, 0x0D):
                text = buf.decode("ascii", "ignore").strip()
                buf.clear()
                if text.startswith("!"):
                    lines.append(text)
                    self.last_control[name] = text
            elif b == 0x7E:
                buf.clear()
            elif 32 <= b < 127:
                if len(buf) < 64:
                    buf.append(b)
        return lines

    def control(self, target: str, text: str) -> None:
        """Send a control line (e.g. !BOOT) to one endpoint's agent."""
        self.record_raw(target, "out", (text + "\n").encode())
        self.endpoints[target].send((text + "\n").encode())
        self.note(f"-> {target}: {text}", kind="info")

    def wait_for(self, target: str, token: str, timeout: float = 120.0) -> bool:
        """Block until the agent reports `token`, or give up."""
        import time as _t

        deadline = _t.monotonic() + timeout
        while _t.monotonic() < deadline:
            if self.last_control.get(target) == token:
                return True
            _t.sleep(0.2)
        return False

    def send(self, target: str, data: bytes, label: str = "host",
             lead_flag: bool = True) -> None:
        """Put raw bytes on one endpoint, logging each frame inside them.

        An extra 0x7E is prepended by default. The agent's sequence counter
        starts from whatever the register holds, so its very first byte can
        collide with the number the MCU last saw and be ignored as a repeat.
        If that byte is the opening flag the whole frame is lost. A duplicate
        flag costs nothing - the receiver reads it as an empty frame - and
        guarantees a real one arrives.
        """
        ep = self.endpoints[target]
        if lead_flag and data[:1] == b"\x7e":
            data = b"\x7e" + data
        self.record_raw(target, "out", data)
        ep.send(data)
        self.counters["host"] += 1
        for msg in protocol.FrameDecoder().feed(data):
            self._emit(LogEntry(
                seq=next(self._seq), t=time.time(), source=label, target=target,
                direction=f"{label} -> {target}", summary=msg.summary(),
                kind=msg.kind, hexdump=msg.hex(), name=msg.name, relayed=True,
            ))

    def wait_response(self, code: int, timeout: float = 20.0):
        """Block until a response with this SPDM code arrives, or give up.

        Returns the Message, or None on timeout. SPDM is request/response:
        the next request must not go out until this one is answered.
        """
        import time as _t

        start = _t.monotonic()
        seen = self._seq_seen()
        while _t.monotonic() - start < timeout:
            with self._lock:
                for e in self.log:
                    if e.seq <= seen:
                        continue
                    msg = self._responses.get(e.seq)
                    if msg is not None and msg.spdm_code in (code, 0x7F):
                        return msg
            _t.sleep(0.05)
        return None

    def _seq_seen(self) -> int:
        with self._lock:
            return self.log[-1].seq if self.log else 0

    def note(self, text: str, kind: str = "info") -> None:
        self._emit(LogEntry(
            seq=next(self._seq), t=time.time(), source="bridge", target="-",
            direction="bridge", summary=text, kind=kind, hexdump="", name="note",
        ))

    # ----------------------------------------------------------------- info
    def snapshot(self, since: int = 0) -> List[dict]:
        with self._lock:
            return [e.as_dict() for e in self.log if e.seq > since]

    def status(self) -> dict:
        return {
            "endpoints": {
                name: {
                    "transport": type(ep).__name__,
                    "status": ep.status(),
                    "can_receive": ep.can_receive,
                }
                for name, ep in self.endpoints.items()
            },
            "counters": dict(self.counters),
            "relay": self.relay,
            "responder_mode": self.responder_mode,
            "uptime": int(time.time() - self.started_at) if self.started_at else 0,
        }
