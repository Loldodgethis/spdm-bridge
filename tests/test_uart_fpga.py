"""
Board-side tests for the Caliptra MCU "UART" on the FPGA. Run ON THE BOARD as root.

Three groups, each skips cleanly when its prerequisites are missing:

  1. Firmware source checks   (needs MCU_SW_DIR, default ~/caliptra-mcu-sw)
  2. UIO / log FIFO checks     (needs /dev/uio*; FIFO tests need UART_FIFO_OFFSET)
  3. End-to-end log capture    (needs UART_TEST_CMD)

Examples:
    pytest -v test_uart_fpga.py
    UART_FIFO_OFFSET=0x1000 pytest -v test_uart_fpga.py -k fifo
    UART_TEST_CMD="cargo xtask ..." UART_EXPECT="Hello" pytest -v -k e2e

WARNING: log FIFO reads are destructive. Do not run the FIFO group while the
hw-model / xtask process is running, or both will lose bytes.

FIFO bit layout below is ASSUMED (CharValid=bit 8, Empty=bit 0, Full=bit 1).
Confirm against hw/model/src/fpga_regs.rs and override via env vars if different.
"""
import mmap
import os
import re
import struct
import subprocess
import time
from pathlib import Path

import pytest

MCU_SW_DIR = Path(os.environ.get("MCU_SW_DIR", Path.home() / "caliptra-mcu-sw"))
UART_ADDR = 0xA4011014
VALID_BIT = 0x100

FIFO_UIO = os.environ.get("UART_FIFO_UIO", "/dev/uio0")
FIFO_MAP = int(os.environ.get("UART_FIFO_MAP", "0"))
FIFO_OFFSET = os.environ.get("UART_FIFO_OFFSET")  # offset of log_fifo_data inside the map
BIT_CHAR_VALID = int(os.environ.get("UART_BIT_CHAR_VALID", "8"))
BIT_EMPTY = int(os.environ.get("UART_BIT_EMPTY", "0"))
BIT_FULL = int(os.environ.get("UART_BIT_FULL", "1"))


def _num(s: str) -> int:
    return int(s.replace("_", ""), 0)


# =====================================================================
# 1. Firmware source checks
# =====================================================================
needs_src = pytest.mark.skipif(not MCU_SW_DIR.is_dir(), reason=f"{MCU_SW_DIR} not found")


def _rs_files():
    return [p for p in MCU_SW_DIR.rglob("*.rs") if "target" not in p.parts]


@needs_src
def test_src_uart_address_consistent():
    """Every FPGA_UART_OUTPUT definition must point at the same register."""
    pat = re.compile(r"FPGA_UART_OUTPUT\s*:\s*\*mut\s+u32\s*=\s*(0x[0-9a-fA-F_]+)")
    found = {}
    for f in _rs_files():
        for m in pat.finditer(f.read_text(errors="ignore")):
            found[str(f.relative_to(MCU_SW_DIR))] = _num(m.group(1))
    assert found, "no FPGA_UART_OUTPUT definitions found"
    assert set(found.values()) == {UART_ADDR}, found


@needs_src
def test_src_writes_set_valid_bit():
    """Every write to FPGA_UART_OUTPUT must OR in the 0x100 strobe bit."""
    pat = re.compile(r"write_volatile\(\s*FPGA_UART_OUTPUT\s*,([^;]*)\)")
    bad, count = [], 0
    for f in _rs_files():
        for m in pat.finditer(f.read_text(errors="ignore")):
            count += 1
            if "0x100" not in m.group(1):
                bad.append(f"{f.relative_to(MCU_SW_DIR)}: {m.group(0)}")
    assert count > 0
    assert not bad, bad


@needs_src
def test_src_rx_is_stubbed():
    """Documents current behavior: FPGA receive path always returns 0.
    If this FAILS, someone implemented RX. Good news; update your design and this test."""
    io_rs = MCU_SW_DIR / "platforms/fpga/runtime/src/io.rs"
    text = io_rs.read_text()
    assert re.search(r"fn\s+read_byte\(\)\s*->\s*u8\s*\{\s*0\s*\}", text), \
        "read_byte() is no longer a stub - RX may be implemented"


# =====================================================================
# 2. UIO and log FIFO
# =====================================================================
needs_uio = pytest.mark.skipif(not Path("/sys/class/uio").is_dir(), reason="no UIO on this host")


def _uio_maps(dev: str):
    base = Path("/sys/class/uio") / Path(dev).name / "maps"
    maps = []
    for m in sorted(base.glob("map*"), key=lambda p: int(p.name[3:])):
        maps.append((int((m / "addr").read_text(), 16), int((m / "size").read_text(), 16)))
    return maps


@needs_uio
def test_uio_devices_present():
    names = {p.name: (p / "name").read_text().strip() for p in Path("/sys/class/uio").iterdir()}
    assert any("caliptra-fpga" in n for n in names.values()), names


@needs_uio
def test_uio_maps_sane():
    for dev in ("/dev/uio0", "/dev/uio1"):
        if not Path(dev).exists():
            continue
        maps = _uio_maps(dev)
        assert maps, f"{dev} has no maps"
        for addr, size in maps:
            assert size > 0 and size % mmap.PAGESIZE == 0, (dev, hex(addr), hex(size))


@needs_uio
def test_uio_mcu_uart_register_region_is_mapped():
    """0xa4011014 (MCU view) should fall inside some UIO region (ARM view).
    If this fails the address spaces differ; note the mapping, don't 'fix' the test."""
    regions = [m for d in ("/dev/uio0", "/dev/uio1") if Path(d).exists() for m in _uio_maps(d)]
    assert any(a <= UART_ADDR < a + s for a, s in regions), \
        [f"{hex(a)}+{hex(s)}" for a, s in regions]


class LogFifo:
    def __init__(self):
        addr, size = _uio_maps(FIFO_UIO)[FIFO_MAP]
        self.fd = os.open(FIFO_UIO, os.O_RDWR | os.O_SYNC)
        self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE,
                            offset=FIFO_MAP * mmap.PAGESIZE)
        self.off = _num(FIFO_OFFSET)

    def _r32(self, off):
        return struct.unpack_from("<I", self.mm, off)[0]

    def status(self):
        return self._r32(self.off + 4)

    def read_data(self):
        return self._r32(self.off)  # destructive pop

    def drain(self, timeout=2.0, max_bytes=1 << 16):
        out, deadline = bytearray(), time.monotonic() + timeout
        while time.monotonic() < deadline and len(out) < max_bytes:
            d = self.read_data()
            if d & (1 << BIT_CHAR_VALID):
                out.append(d & 0xFF)
            else:
                time.sleep(0.01)
        return bytes(out)

    def close(self):
        self.mm.close()
        os.close(self.fd)


needs_fifo = pytest.mark.skipif(FIFO_OFFSET is None, reason="set UART_FIFO_OFFSET (see fpga_regs.rs)")


@pytest.fixture
def fifo():
    f = LogFifo()
    yield f
    f.close()


@needs_uio
@needs_fifo
def test_fifo_status_not_empty_and_full(fifo):
    s = fifo.status()
    assert not (s & (1 << BIT_EMPTY) and s & (1 << BIT_FULL)), hex(s)


@needs_uio
@needs_fifo
def test_fifo_data_upper_bits_clean(fifo):
    """Only NextChar + CharValid should ever be set in log_fifo_data."""
    allowed = 0xFF | (1 << BIT_CHAR_VALID)
    for _ in range(64):
        d = fifo.read_data()
        assert d & ~allowed == 0, hex(d)


@needs_uio
@needs_fifo
def test_fifo_empty_after_drain(fifo):
    fifo.drain(timeout=1.0)
    assert fifo.status() & (1 << BIT_EMPTY), "FIFO still reports data after drain"


@needs_uio
@needs_fifo
def test_fifo_output_is_text(fifo):
    """Boot firmware first (without hw-model reading), then run this."""
    data = fifo.drain(timeout=3.0)
    if not data:
        pytest.skip("FIFO empty - start MCU firmware first")
    printable = sum(32 <= b < 127 or b in b"\r\n\t" for b in data)
    assert printable / len(data) > 0.95, data[:200]


# =====================================================================
# 3. End-to-end: run real firmware, check its UART log
# =====================================================================
E2E_CMD = os.environ.get("UART_TEST_CMD")
E2E_EXPECT = os.environ.get("UART_EXPECT")
E2E_TIMEOUT = int(os.environ.get("UART_TIMEOUT", "300"))


@pytest.fixture(scope="module")
def e2e_output():
    if not E2E_CMD:
        pytest.skip("set UART_TEST_CMD to the command that boots MCU firmware")
    p = subprocess.run(E2E_CMD, shell=True, cwd=MCU_SW_DIR, capture_output=True,
                       timeout=E2E_TIMEOUT)
    return p.returncode, p.stdout + p.stderr


def test_e2e_command_succeeds(e2e_output):
    rc, out = e2e_output
    assert rc == 0, out[-2000:].decode(errors="replace")


def test_e2e_expected_text(e2e_output):
    if not E2E_EXPECT:
        pytest.skip("set UART_EXPECT to a string the firmware prints")
    assert re.search(E2E_EXPECT.encode(), e2e_output[1]), "expected text not in UART log"


def test_e2e_no_strobe_bit_leaks(e2e_output):
    """If 0x100 handling broke, you'd see NULs or 0x01 bytes in the text."""
    out = e2e_output[1]
    assert b"\x00" not in out
