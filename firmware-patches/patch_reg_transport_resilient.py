#!/usr/bin/env python3
"""
patch_reg_transport_resilient.py - stop a bad frame from killing the responder.

Run from the root of caliptra-mcu-sw on the LAPTOP:
    python3 patch_reg_transport_resilient.py
    cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes

The bug
-------
recv_request returned Err on a bad FCS, an unexpected message type, or an
oversized frame. spdm-lib propagates that out of its run loop, so ONE bad
frame ends the responder for the rest of the boot: the transport still acks
bytes (its receive code is still mapped) but nothing ever answers again.

That matches what the hardware did: a VERSION response, then three ERRORs,
then permanent silence - and every later run silent from the start, because
a stray byte left over in the register was enough to kill the loop before the
first real request arrived.

The fix
-------
Malformed frames are skipped and the loop keeps waiting, which is what a
transport is supposed to do - framing errors are a wire problem, not a reason
to tear down the responder. Only a caller error (a buffer too small to hold
even a header) is still reported, because that one is not recoverable here.
"""
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/spdm/reg_transport.rs"
try:
    s = open(P).read()
except OSError:
    sys.exit("run from the root of caliptra-mcu-sw (reg_transport.rs not found)")

if "frames are skipped" in s:
    sys.exit("already patched")

# ------------------------------------------------- oversized frame: skip it
old = """            if overflow {
                return Err(error_code::MESSAGE_TOO_LARGE);
            }"""
new = """            // Malformed frames are skipped, never reported: returning Err
            // here would end spdm-lib's run loop and the responder would be
            // gone for the rest of the boot.
            if overflow {
                continue;
            }"""
assert s.count(old) == 1, "overflow anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------------- bad FCS: skip it
old = """            if fcs16(&buf[..body]) != want {
                return Err(error_code::BAD_CHECKSUM);
            }
            if buf[0] & 0x7F != MSG_TYPE_SPDM {
                return Err(error_code::UNEXPECTED_MESSAGE_TYPE);
            }"""
new = """            if fcs16(&buf[..body]) != want {
                continue; // corrupt on the wire; wait for the next frame
            }
            if buf[0] & 0x7F != MSG_TYPE_SPDM {
                continue; // not for us; wait for the next frame
            }"""
assert s.count(old) == 1, "validation anchor not unique"
s = s.replace(old, new)

# ---------------------------------------- note the behaviour in the doc block
old = """    /// Reads one framed message. On return `buf[0]` is the message-type byte
    /// and `buf[1..len]` is the SPDM payload, as the trait requires."""
new = """    /// Reads one framed message. On return `buf[0]` is the message-type byte
    /// and `buf[1..len]` is the SPDM payload, as the trait requires.
    ///
    /// Bad frames are skipped and the wait continues: an Err here would end
    /// spdm-lib's run loop, taking the responder down for the rest of the
    /// boot."""
assert s.count(old) == 1, "doc anchor not unique"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
print()
print("rebuild:  cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes")
