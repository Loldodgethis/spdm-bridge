#!/usr/bin/env python3
"""
patch_reg_transport_debug.py - find out why responses stop, and stop one
missing acknowledgement from wedging the responder.

Run from the root of caliptra-mcu-sw on the LAPTOP:
    python3 patch_reg_transport_debug.py
    cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes

Two changes.

1. LOGGING. The firmware now prints on every receive and send:

       SPDM_REG: rx frame, N bytes, code 0xNN
       SPDM_REG: tx start, N bytes
       SPDM_REG: tx done, N bytes sent
       SPDM_REG: tx byte NOT acked after <limit> polls - host not reading

   That distinguishes the three possibilities we cannot currently tell apart:
   the stack never calls send_response, it calls it and blocks, or it sends
   and the host misses the bytes.

2. A BOUND ON THE ACK WAIT. send_byte used to spin forever waiting for the
   host to echo the sequence number. If nothing is reading - the agent
   stopped, the Pi disconnected - that call never returns, so the responder
   is stuck inside send_response and every later request goes unanswered
   while the receive side still acks bytes. That matches the symptom exactly:
   bytes accepted, nothing ever sent back.

   Now it gives up on a byte after TX_ACK_LIMIT polls, logs it, and carries
   on, so the responder survives a host that stops listening.
"""
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/spdm/reg_transport.rs"
try:
    s = open(P).read()
except OSError:
    sys.exit("run from the root of caliptra-mcu-sw (reg_transport.rs not found)")

if "SPDM_REG: tx start" in s:
    sys.exit("already patched")

# ------------------------------------------------------------------ imports
old = """use async_trait::async_trait;
use caliptra_mcu_spdm_traits::{McuResult, SpdmPalIoKind, SpdmPalTransport};"""
new = """use async_trait::async_trait;
use caliptra_mcu_libsyscall_caliptra::DefaultSyscalls;
use caliptra_mcu_libtock_console::Console;
use core::fmt::Write as _;

use caliptra_mcu_spdm_traits::{McuResult, SpdmPalIoKind, SpdmPalTransport};"""
assert s.count(old) == 1, "import anchor not unique"
s = s.replace(old, new)

# -------------------------------------------- bound the wait for an ack
old = """    /// Sends one byte, yielding until the host acknowledges it.
    async fn send_byte(&mut self, b: u8) {
        self.tx_seq = (self.tx_seq + 1) & 0xFF;
        self.publish(b);
        let mut polls: u32 = 0;
        loop {
            // SAFETY: fixed MMIO address inside the user-accessible PMP region.
            let v = unsafe { core::ptr::read_volatile(RX) };
            if ((v >> 24) & 0xFF) == self.tx_seq {
                return;
            }
            polls = polls.wrapping_add(1);
            if polls % POLLS_PER_YIELD == 0 {
                yield_now().await;
            }
        }
    }"""
new = """    /// Sends one byte, yielding until the host acknowledges it.
    ///
    /// Gives up after `TX_ACK_LIMIT` polls. Waiting forever would wedge the
    /// responder inside `send_response` whenever the host stops reading -
    /// the receive side would keep acking while nothing was ever answered.
    async fn send_byte(&mut self, b: u8) -> bool {
        self.tx_seq = (self.tx_seq + 1) & 0xFF;
        self.publish(b);
        let mut polls: u32 = 0;
        loop {
            // SAFETY: fixed MMIO address inside the user-accessible PMP region.
            let v = unsafe { core::ptr::read_volatile(RX) };
            if ((v >> 24) & 0xFF) == self.tx_seq {
                return true;
            }
            polls = polls.wrapping_add(1);
            if polls >= TX_ACK_LIMIT {
                let mut cw = Console::<DefaultSyscalls>::writer();
                let _ = writeln!(
                    cw,
                    "SPDM_REG: tx byte NOT acked after {} polls - host not reading",
                    TX_ACK_LIMIT
                );
                return false;
            }
            if polls % POLLS_PER_YIELD == 0 {
                yield_now().await;
            }
        }
    }"""
assert s.count(old) == 1, "send_byte anchor not unique"
s = s.replace(old, new)

# the limit itself
old = """/// Polls before handing the CPU back, so the executor is not starved while
/// waiting for a byte that may be seconds away.
const POLLS_PER_YIELD: u32 = 64;"""
new = """/// Polls before handing the CPU back, so the executor is not starved while
/// waiting for a byte that may be seconds away.
const POLLS_PER_YIELD: u32 = 64;

/// How long to wait for the host to acknowledge one outgoing byte before
/// giving up on it. Generous, but finite.
const TX_ACK_LIMIT: u32 = 20_000_000;"""
assert s.count(old) == 1, "const anchor not unique"
s = s.replace(old, new)

# send_escaped returns success too
old = """    /// Sends a byte with 0x7D escaping applied.
    async fn send_escaped(&mut self, b: u8) {
        if b == FLAG || b == ESCAPE {
            self.send_byte(ESCAPE).await;
            self.send_byte(b ^ 0x20).await;
        } else {
            self.send_byte(b).await;
        }
    }"""
new = """    /// Sends a byte with 0x7D escaping applied.
    async fn send_escaped(&mut self, b: u8) -> bool {
        if b == FLAG || b == ESCAPE {
            self.send_byte(ESCAPE).await && self.send_byte(b ^ 0x20).await
        } else {
            self.send_byte(b).await
        }
    }"""
assert s.count(old) == 1, "send_escaped anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------------- log on receive
old = """            // buf[0] stays as the transport header; payload follows it.
            return Ok((SpdmPalIoKind::Message, body));"""
new = """            {
                let mut cw = Console::<DefaultSyscalls>::writer();
                let code = if body > 2 { buf[2] } else { 0xFF };
                let _ = writeln!(
                    cw,
                    "SPDM_REG: rx frame, {} bytes, code {:#04x}",
                    body, code
                );
            }

            // buf[0] stays as the transport header; payload follows it.
            return Ok((SpdmPalIoKind::Message, body));"""
assert s.count(old) == 1, "recv return anchor not unique"
s = s.replace(old, new)

# -------------------------------------------------------- log on send
old = """        msg[0] = MSG_TYPE_SPDM;
        let fcs = fcs16(msg);

        self.send_byte(FLAG).await;
        for i in 0..msg.len() {
            let b = msg[i];
            self.send_escaped(b).await;
        }
        self.send_escaped((fcs >> 8) as u8).await;
        self.send_escaped((fcs & 0xFF) as u8).await;
        self.send_byte(FLAG).await;
        Ok(())"""
new = """        msg[0] = MSG_TYPE_SPDM;
        let fcs = fcs16(msg);

        {
            let mut cw = Console::<DefaultSyscalls>::writer();
            let code = if msg.len() > 2 { msg[2] } else { 0xFF };
            let _ = writeln!(
                cw,
                "SPDM_REG: tx start, {} bytes, code {:#04x}",
                msg.len(),
                code
            );
        }

        let mut sent = 0usize;
        let mut ok = self.send_byte(FLAG).await;
        if ok {
            for i in 0..msg.len() {
                let b = msg[i];
                if !self.send_escaped(b).await {
                    ok = false;
                    break;
                }
                sent += 1;
            }
        }
        if ok {
            ok = self.send_escaped((fcs >> 8) as u8).await
                && self.send_escaped((fcs & 0xFF) as u8).await
                && self.send_byte(FLAG).await;
        }

        {
            let mut cw = Console::<DefaultSyscalls>::writer();
            let _ = writeln!(
                cw,
                "SPDM_REG: tx done, {}/{} bytes sent, complete={}",
                sent,
                msg.len(),
                ok
            );
        }

        // A host that stopped reading is not a stack error: report success so
        // spdm-lib keeps serving, and let the next request start cleanly.
        Ok(())"""
assert s.count(old) == 1, "send body anchor not unique"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
print()
print("rebuild:  cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes")
print()
print("then watch for, in order:")
print("  SPDM_REG: rx frame, ...     the request arrived")
print("  SPDM_REG: tx start, ...     spdm-lib produced an answer")
print("  SPDM_REG: tx done, ...      whether every byte was acknowledged")
print()
print("no 'tx start' => the stack never answered (look above it for its own error)")
print("'tx start' but no 'tx done' => still blocked somewhere in the send")
print("'complete=false' => the host side is not acknowledging; agent not running?")
