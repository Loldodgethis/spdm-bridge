#!/usr/bin/env python3
"""
patch_echo_ack.py - add flow control to the echo loop.

Run from the root of caliptra-mcu-sw on the LAPTOP, after patch_echo_yield.py:
    python3 patch_echo_ack.py

Problem: with yields enabled the app only gets the CPU in slices. Between
slices the host writes several bytes and the app sees only the last one, so
bytes are lost at ANY send rate - slowing the host down does not help.

Fix: after consuming a byte, the app writes the sequence number it just took
into mci_generic_input_wires[1] (0xa4010130), low 8 bits. The host polls that
register and only sends the next byte once its own sequence number comes back.
Transfer speed then matches whatever the MCU can actually do.

The top bits of 0xa4010130 are preserved (0xC0000000), since MCU ROM reads
bits 29..31 of that word during boot.

Use with the matching uart_tx.py --ack mode.

Undo with:
    git checkout platforms/emulator/runtime/userspace/apps/user/src/main.rs
"""
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/main.rs"
s = open(P).read()

if "POLLS_PER_YIELD" not in s:
    sys.exit("run patch_echo_yield.py first")
if "ACK_TOP" in s:
    sys.exit("already patched")

# ---------------------------------------------------------------- ack target
old = """    // Hand the CPU back this often so the kernel and other apps can run.
    const POLLS_PER_YIELD: u32 = 64;"""
new = """    // Hand the CPU back this often so the kernel and other apps can run.
    const POLLS_PER_YIELD: u32 = 64;
    // Where we acknowledge the byte we just consumed, so the host knows when
    // to send the next one. Low 8 bits = sequence number just taken.
    // Top bits keep the value MCU ROM expects to read at boot.
    const ACK: *mut u32 = 0xa401_0130 as *mut u32;
    const ACK_TOP: u32 = 0xC000_0000;"""
assert s.count(old) == 1, "yield-const anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------- write the ack after taking
old = """        last_seq = seq;
        total = total.wrapping_add(1);"""
new = """        last_seq = seq;
        total = total.wrapping_add(1);

        // Tell the host this byte was taken; it waits for this before
        // sending the next one.
        unsafe { core::ptr::write_volatile(ACK, ACK_TOP | seq) };"""
assert s.count(old) == 1, "consume anchor not unique"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
