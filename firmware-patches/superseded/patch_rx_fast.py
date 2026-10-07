#!/usr/bin/env python3
"""
patch_rx_fast.py - make the MCU UART receive path fast enough for a byte stream.

Run from the root of caliptra-mcu-sw on the LAPTOP, AFTER patch_rx.py:
    python3 patch_rx_fast.py

Problem: the alarm handler polled the register once per timer expiry, so each
byte cost a full app-read -> arm-timer -> fire -> deliver round trip, about
17M cycles (~1 byte/sec). A host sending faster than that loses bytes.

Fix: when the alarm fires, spin on the register for a bounded number of
iterations, draining bytes into the receive buffer as they land. Only fall
back to re-arming the timer once the line has been quiet for the whole spin
budget. Bytes in a burst are then picked up at polling speed instead of
timer speed.

SPIN_LIMIT is the quiet-time budget per alarm. Raise it if the host still
outruns the MCU; lower it if the kernel feels starved.

Undo with:
    git checkout platforms/fpga/runtime/src/io.rs
"""
import sys

P = "platforms/fpga/runtime/src/io.rs"
s = open(P).read()

if "FPGA_UART_INPUT" not in s:
    sys.exit("run patch_rx.py first")
if "RX_SPIN_LIMIT" in s:
    sys.exit("already patched")

# ------------------------------------------------------------- spin constant
old = 'const FPGA_UART_INPUT: *const u32 = 0xa401_012c as *const u32;'
new = old + """
/// How many empty polls to spin through before giving the timer back.
const RX_SPIN_LIMIT: u32 = 200_000;"""
assert s.count(old) == 1, "FPGA_UART_INPUT anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------------- draining alarm body
old = """        let b = self.poll_byte();
        if b == 0 {
            self.set_alarm(self.alarm.minimum_dt().into_u64());
            return;
        }

        if let Some(rx_buffer) = self.rx_buffer.take() {
            let len = self.rx_len.get();
            let mut index = self.rx_index.get();
            if index < len {
                rx_buffer[index] = b;
                index += 1;
            }
            if index >= len {
                self.rx_index.set(0);
                self.rx_client.map(move |client| {
                    client.received_buffer(rx_buffer, len, Ok(()), hil::uart::Error::None);
                });
            } else {
                self.rx_index.set(index);
                self.rx_buffer.replace(rx_buffer);
                self.set_alarm(self.alarm.minimum_dt().into_u64());
            }
        }"""
new = """        let Some(rx_buffer) = self.rx_buffer.take() else {
            return;
        };
        let len = self.rx_len.get();
        let mut index = self.rx_index.get();
        let mut quiet: u32 = 0;

        // Drain the burst: keep polling while bytes keep arriving, so a
        // stream moves at polling speed rather than one byte per timer tick.
        while index < len {
            let b = self.poll_byte();
            if b == 0 {
                quiet += 1;
                if quiet >= RX_SPIN_LIMIT {
                    break;
                }
                continue;
            }
            quiet = 0;
            rx_buffer[index] = b;
            index += 1;
        }

        if index >= len {
            self.rx_index.set(0);
            self.rx_client.map(move |client| {
                client.received_buffer(rx_buffer, len, Ok(()), hil::uart::Error::None);
            });
        } else {
            self.rx_index.set(index);
            self.rx_buffer.replace(rx_buffer);
            self.set_alarm(self.alarm.minimum_dt().into_u64());
        }"""
assert s.count(old) == 1, "alarm body anchor not found - is patch_rx.py applied?"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
