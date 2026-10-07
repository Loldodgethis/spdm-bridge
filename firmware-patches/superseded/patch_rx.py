#!/usr/bin/env python3
"""
patch_rx.py - give the FPGA MCU runtime a real UART receive path.

Run from the root of caliptra-mcu-sw on the LAPTOP (where builds happen):
    python3 patch_rx.py

Bytes arrive through the FPGA wrapper register mci_generic_input_wires[0]
(0xa401012c), which the ARM side (or uart_tx.py) writes:

    bits  0..7   the byte
    bit   8      valid
    bits 16..23  sequence counter, incremented per byte so the MCU can tell
                 a new byte from the same one still sitting in the register

Undo with:
    git checkout platforms/fpga/runtime/src/io.rs
"""
import sys

P = "platforms/fpga/runtime/src/io.rs"
s = open(P).read()

if "FPGA_UART_INPUT" in s:
    sys.exit("already patched")

# ---------------------------------------------------------------- 1. address
old = 'const FPGA_UART_OUTPUT: *mut u32 = 0xa401_1014 as *mut u32;'
new = old + """
// Host -> MCU byte channel: FPGA wrapper mci_generic_input_wires[0].
// Layout: [7:0] byte, [8] valid, [23:16] sequence counter.
const FPGA_UART_INPUT: *const u32 = 0xa401_012c as *const u32;"""
assert s.count(old) == 1, "FPGA_UART_OUTPUT anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------- 2. per-instance seq field
old = """    tx_buffer: TakeCell<'static, [u8]>,
    tx_len: Cell<usize>,
    deferred_call: DeferredCall,
}"""
new = """    tx_buffer: TakeCell<'static, [u8]>,
    tx_len: Cell<usize>,
    deferred_call: DeferredCall,
    last_rx_seq: Cell<u32>,
}"""
assert s.count(old) == 1, "struct field anchor not unique"
s = s.replace(old, new)

old = """            tx_len: Cell::new(0),
            deferred_call: DeferredCall::new(),
        }"""
new = """            tx_len: Cell::new(0),
            deferred_call: DeferredCall::new(),
            last_rx_seq: Cell::new(0xffff_ffff),
        }"""
assert s.count(old) == 1, "constructor anchor not unique"
s = s.replace(old, new)

# -------------------------------------------------------- 3. poll_byte method
old = """    pub fn handle_interrupt(&self) {"""
new = """    /// Poll the host -> MCU register. Returns 0 when no NEW byte is waiting.
    ///
    /// # Safety
    /// Reads a memory-mapped register.
    fn poll_byte(&self) -> u8 {
        let v = unsafe { core::ptr::read_volatile(FPGA_UART_INPUT) };
        if v & 0x100 == 0 {
            return 0;
        }
        let seq = (v >> 16) & 0xff;
        if self.last_rx_seq.get() == seq {
            return 0;
        }
        self.last_rx_seq.set(seq);
        (v & 0xff) as u8
    }

    pub fn handle_interrupt(&self) {"""
assert s.count(old) == 1, "handle_interrupt anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------------ 4. real alarm handler
old = """impl<'a> AlarmClient for SemihostUart<'a> {
    fn alarm(&self) {
        // Callback to the clients
        if let Some(rx_buffer) = self.rx_buffer.take() {
            self.rx_client.map(move |client| {
                client.received_buffer(
                    rx_buffer,
                    self.rx_len.get(),
                    Ok(()),
                    hil::uart::Error::None,
                );
            });
        }
    }
}"""
new = """impl<'a> AlarmClient for SemihostUart<'a> {
    fn alarm(&self) {
        // Poll the host -> MCU register. Deliver to the client only once we
        // actually have the bytes the client asked for; otherwise keep polling.
        let b = self.poll_byte();
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
        }
    }
}"""
assert s.count(old) == 1, "alarm anchor not unique"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
