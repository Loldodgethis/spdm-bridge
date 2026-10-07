#!/usr/bin/env python3
"""
patch_echo_yield.py - let the rest of the system run while the echo loop polls.

Run from the root of caliptra-mcu-sw on the LAPTOP, after patch_echo_direct.py:
    python3 patch_echo_yield.py

Problem: the direct-poll loop never yields, so the kernel and the other apps
get no CPU and the test never finishes - you have to Ctrl+C it. (The earlier
Console::read version yielded on every byte, which is why those runs completed.)

Fix: call yield_no_wait() every POLLS_PER_YIELD polls. It returns immediately
when there is no pending upcall, so throughput stays high, but the kernel gets
a chance to run.

Undo with:
    git checkout platforms/emulator/runtime/userspace/apps/user/src/main.rs
"""
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/main.rs"
s = open(P).read()

if "direct-poll" not in s:
    sys.exit("run patch_echo_direct.py first")
if "POLLS_PER_YIELD" in s:
    sys.exit("already patched")

# ------------------------------------------------------------ import + const
old = """    type Con = Console<DefaultSyscalls>;

    // Host -> MCU byte register in the FPGA wrapper (user-accessible MMIO).
    // [7:0] byte, [8] valid, [23:16] sequence counter.
    const RX: *const u32 = 0xa401_012c as *const u32;"""
new = """    type Con = Console<DefaultSyscalls>;
    use caliptra_mcu_libtock_platform::Syscalls;

    // Host -> MCU byte register in the FPGA wrapper (user-accessible MMIO).
    // [7:0] byte, [8] valid, [23:16] sequence counter.
    const RX: *const u32 = 0xa401_012c as *const u32;
    // Hand the CPU back this often so the kernel and other apps can run.
    const POLLS_PER_YIELD: u32 = 64;"""
assert s.count(old) == 1, "header anchor not unique"
s = s.replace(old, new)

# ----------------------------------------------------------- yielding in loop
old = """    loop {
        let v = unsafe { core::ptr::read_volatile(RX) };"""
new = """    let mut polls: u32 = 0;
    loop {
        polls = polls.wrapping_add(1);
        if polls % POLLS_PER_YIELD == 0 {
            // Without this the kernel never runs and tests never complete.
            DefaultSyscalls::yield_no_wait();
        }

        let v = unsafe { core::ptr::read_volatile(RX) };"""
assert s.count(old) == 1, "loop anchor not unique"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
