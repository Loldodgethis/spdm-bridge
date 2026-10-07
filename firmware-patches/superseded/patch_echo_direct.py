#!/usr/bin/env python3
"""
patch_echo_direct.py - make the echo loop poll the RX register directly.

Run from the root of caliptra-mcu-sw on the LAPTOP, after patch_echo.py:
    python3 patch_echo_direct.py

Why: going through Console::read costs ~16.7M cycles per byte (app syscall ->
arm alarm -> timer fires -> deliver), and that period doesn't change when the
host sends faster, so it is the kernel round trip, not the channel.

The app's PMP map includes UserMMIO(0xa4010000+0x2000) as user-accessible, so
userspace can read the RX register itself. This replaces the loop body with a
tight poll of 0xa401012c - no syscall per byte.

Output is buffered per line instead of echoed per byte, because each console
write goes out the debug FIFO one byte at a time and interleaves with other
log lines.

Undo with:
    git checkout platforms/emulator/runtime/userspace/apps/user/src/main.rs
"""
import re
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/main.rs"
s = open(P).read()

if "UART_ECHO_TEST" not in s:
    sys.exit("run patch_echo.py first (no UART_ECHO_TEST found)")
if "direct-poll" in s:
    sys.exit("already patched")

new_fn = r'''fn uart_echo_loop() -> ! {
    use caliptra_mcu_libsyscall_caliptra::DefaultSyscalls;
    use caliptra_mcu_libtock_console::Console;
    type Con = Console<DefaultSyscalls>;

    // Host -> MCU byte register in the FPGA wrapper (user-accessible MMIO).
    // [7:0] byte, [8] valid, [23:16] sequence counter.
    const RX: *const u32 = 0xa401_012c as *const u32;

    let _ = Con::write(b"[uart-echo] direct-poll loop running\n");

    let mut last_seq: u32 = 0xffff_ffff;
    let mut total: u32 = 0;
    let mut line = [0u8; 96];
    let mut n: usize = 0;

    loop {
        let v = unsafe { core::ptr::read_volatile(RX) };
        if v & 0x100 == 0 {
            continue;
        }
        let seq = (v >> 16) & 0xff;
        if seq == last_seq {
            continue;
        }
        last_seq = seq;
        total = total.wrapping_add(1);

        let b = (v & 0xff) as u8;
        if b == b'\n' || b == b'\r' || n == line.len() {
            let text = core::str::from_utf8(&line[..n]).unwrap_or("<non-utf8>");
            let _ = writeln!(Con::writer(), "[uart-echo] rx#{} line: {:?}", total, text);
            n = 0;
        } else {
            line[n] = b;
            n += 1;
        }
    }
}'''

# Replace the whole existing uart_echo_loop function.
m = re.search(r"fn uart_echo_loop\(\) -> ! \{.*?\n\}\n", s, re.S)
if not m:
    sys.exit("could not locate uart_echo_loop - paste main.rs so it can be adjusted")
s = s[: m.start()] + new_fn + "\n" + s[m.end() :]

open(P, "w").write(s)
print("patched", P)
