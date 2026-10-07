#!/usr/bin/env python3
"""
patch_reg_spdm_transport.py - route the USB register channel into the REAL
Caliptra SPDM responder instead of the hand-written stub.

Run from the root of caliptra-mcu-sw on the LAPTOP:
    python3 patch_reg_spdm_transport.py

What this replaces
------------------
The stub answered a handful of request codes with hardcoded bytes. That is
thrown away. Instead the register channel becomes a transport under spdm-lib,
exactly like MCTP and DOE already are:

    USB register channel
          |
    McuSpdmRegTransport      (new - implements SpdmPalTransport)
          |
    McuSpdmPal + SpdmStack   (existing, untouched)
          |
    version / capabilities / algorithms / digests / certificates /
    challenge / measurements / chunking / large responses

So the responder advertises its real capabilities (CERT, CHAL, MEAS_SIG,
ALIAS_CERT, KEY_EX, ENCRYPT, MAC, CHUNK, LARGE_RESP) and produces real
certificates, signatures and measurements.

Wire format on the register channel
-----------------------------------
Unchanged at the byte level (0x7E-delimited, 0x7D escaping, FCS-16), but the
CONTENTS of a frame are now what spdm-lib expects:

    [message type byte 0x05] [SPDM payload ...]

That is one byte of framing, matching MCTP's header_size() of 1 - NOT the
4-byte MCTP transport header. The Pi must stop adding an MCTP header on this
link (see the note printed at the end).

Files
-----
  adds    platforms/emulator/runtime/userspace/apps/user/src/spdm/reg_transport.rs
  edits   platforms/emulator/runtime/userspace/apps/user/src/spdm/mod.rs
  edits   platforms/emulator/runtime/userspace/apps/user/src/main.rs (stub off)

Undo with:
    git checkout platforms/emulator/runtime/userspace/apps/user/src/
"""
import os
import re
import sys

APP = "platforms/emulator/runtime/userspace/apps/user/src"
TRANSPORT = f"{APP}/spdm/reg_transport.rs"
MOD = f"{APP}/spdm/mod.rs"
MAIN = f"{APP}/main.rs"

for p in (MOD, MAIN):
    if not os.path.exists(p):
        sys.exit(f"{p} not found - run from the root of caliptra-mcu-sw")

if os.path.exists(TRANSPORT):
    sys.exit("already patched")

TRANSPORT_RS = r'''// Licensed under the Apache-2.0 license

//! SPDM transport over the FPGA wrapper register channel.
//!
//! Carries SPDM messages between the MCU and a host (the Raspberry Pi,
//! through the VCK190's USB gadget and an agent on the ARM side) using two
//! memory-mapped registers. It implements [`SpdmPalTransport`], so everything
//! above it - negotiation, certificates, challenge, measurements, chunking -
//! is handled by spdm-lib exactly as it is for MCTP and DOE.
//!
//! # Registers
//!
//! ```text
//! 0xa401012c  host -> MCU   [7:0] byte [8] valid [23:16] seq [31:24] ack
//! 0xa4010130  MCU -> host   [7:0] ack  [15:8] byte [23:16] seq
//! ```
//!
//! One byte moves per handshake in each direction: the sender bumps its
//! sequence number, the receiver echoes it back. Nothing is lost regardless
//! of how either side is scheduled.
//!
//! # Framing
//!
//! Messages are delimited on the byte stream the way DSP0253 does it:
//! `0x7E` marks frame boundaries, `0x7D` escapes an occurrence of either
//! byte inside the frame, and an FCS-16 covers the body. Inside the frame
//! the layout is what spdm-lib expects from a transport with
//! `header_size() == 1`:
//!
//! ```text
//! 0x7E | 0x05 | SPDM payload ... | FCS-hi | FCS-lo | 0x7E
//!        ^ message type byte, occupying buf[0]
//! ```

extern crate alloc;

use alloc::boxed::Box;
use core::future::Future;
use core::pin::Pin;
use core::task::{Context, Poll};

use async_trait::async_trait;
use caliptra_mcu_spdm_traits::{McuResult, SpdmPalIoKind, SpdmPalTransport};

use crate::spdm::reg_transport_errors as error_code;

/// host -> MCU register.
const RX: *const u32 = 0xa401_012c as *const u32;
/// MCU -> host register.
const RET: *mut u32 = 0xa401_0130 as *mut u32;
/// Bits 29..31 of the return register are read by MCU ROM at boot.
const RET_TOP: u32 = 0xC000_0000;

const FLAG: u8 = 0x7E;
const ESCAPE: u8 = 0x7D;

/// Transport header: a single message-type byte, as MCTP uses.
const HEADER_SIZE: usize = 1;
/// SPDM message type carried in that byte.
const MSG_TYPE_SPDM: u8 = 0x05;

/// Largest SPDM message this transport will carry in one piece. spdm-lib
/// fragments anything bigger itself, because the responder advertises CHUNK.
const MAX_MSG: usize = 1024;

/// Polls before handing the CPU back, so the executor is not starved while
/// waiting for a byte that may be seconds away.
const POLLS_PER_YIELD: u32 = 64;

/// Cooperative yield: wakes immediately, but lets other tasks run first.
struct YieldNow(bool);

impl Future for YieldNow {
    type Output = ();

    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<()> {
        if self.0 {
            Poll::Ready(())
        } else {
            self.0 = true;
            cx.waker().wake_by_ref();
            Poll::Pending
        }
    }
}

async fn yield_now() {
    YieldNow(false).await
}

/// FCS-16 (RFC 1662), computed without a lookup table.
fn fcs16(data: &[u8]) -> u16 {
    let mut fcs: u16 = 0xFFFF;
    for &b in data {
        let mut v = (fcs ^ b as u16) & 0xFF;
        for _ in 0..8 {
            v = if v & 1 != 0 { (v >> 1) ^ 0x8408 } else { v >> 1 };
        }
        fcs = (fcs >> 8) ^ v;
    }
    !fcs
}

/// SPDM transport over the FPGA register channel.
pub struct McuSpdmRegTransport {
    /// Sequence number of the last byte accepted from the host.
    last_rx_seq: u32,
    /// Sequence number this side last sent.
    tx_seq: u32,
    /// Sequence number this side has acknowledged to the host.
    ack_seq: u32,
}

impl Default for McuSpdmRegTransport {
    fn default() -> Self {
        Self::new()
    }
}

impl McuSpdmRegTransport {
    pub const fn new() -> Self {
        Self {
            last_rx_seq: 0xFFFF_FFFF,
            tx_seq: 0,
            ack_seq: 0,
        }
    }

    /// Publishes the current acknowledgement and outgoing byte state.
    fn publish(&self, byte: u8) {
        let word = RET_TOP | (self.tx_seq << 16) | ((byte as u32) << 8) | (self.ack_seq & 0xFF);
        // SAFETY: fixed MMIO address inside the user-accessible PMP region.
        unsafe { core::ptr::write_volatile(RET, word) };
    }

    /// Takes the next byte from the host, yielding until one arrives.
    async fn recv_byte(&mut self) -> u8 {
        let mut polls: u32 = 0;
        loop {
            // SAFETY: fixed MMIO address inside the user-accessible PMP region.
            let v = unsafe { core::ptr::read_volatile(RX) };
            if v & 0x100 != 0 {
                let seq = (v >> 16) & 0xFF;
                if seq != self.last_rx_seq {
                    self.last_rx_seq = seq;
                    self.ack_seq = seq;
                    self.publish(0);
                    return (v & 0xFF) as u8;
                }
            }
            polls = polls.wrapping_add(1);
            if polls % POLLS_PER_YIELD == 0 {
                yield_now().await;
            }
        }
    }

    /// Sends one byte, yielding until the host acknowledges it.
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
    }

    /// Sends a byte with 0x7D escaping applied.
    async fn send_escaped(&mut self, b: u8) {
        if b == FLAG || b == ESCAPE {
            self.send_byte(ESCAPE).await;
            self.send_byte(b ^ 0x20).await;
        } else {
            self.send_byte(b).await;
        }
    }
}

#[async_trait]
impl SpdmPalTransport for McuSpdmRegTransport {
    fn secure_message_supported(&self) -> bool {
        // One message type on this link; secured messages would need a
        // second type byte and a second stack instance.
        false
    }

    fn mtu(&self) -> usize {
        MAX_MSG - HEADER_SIZE
    }

    fn header_size(&self) -> usize {
        HEADER_SIZE
    }

    /// Reads one framed message. On return `buf[0]` is the message-type byte
    /// and `buf[1..len]` is the SPDM payload, as the trait requires.
    async fn recv_request(&mut self, buf: &mut [u8]) -> McuResult<(SpdmPalIoKind, usize)> {
        if buf.len() < HEADER_SIZE + 2 {
            return Err(error_code::BUFFER_TOO_SMALL);
        }

        loop {
            // wait for a frame to open
            loop {
                if self.recv_byte().await == FLAG {
                    break;
                }
            }

            let mut n: usize = 0;
            let mut esc = false;
            let mut overflow = false;

            loop {
                let b = self.recv_byte().await;
                if b == FLAG {
                    break;
                } else if esc {
                    if n < buf.len() {
                        buf[n] = b ^ 0x20;
                        n += 1;
                    } else {
                        overflow = true;
                    }
                    esc = false;
                } else if b == ESCAPE {
                    esc = true;
                } else if n < buf.len() {
                    buf[n] = b;
                    n += 1;
                } else {
                    overflow = true;
                }
            }

            if overflow {
                return Err(error_code::MESSAGE_TOO_LARGE);
            }
            if n == 0 {
                continue; // empty frame: two flags back to back
            }
            if n < HEADER_SIZE + 2 {
                continue; // too short to hold a type byte and an FCS
            }

            let body = n - 2;
            let want = ((buf[body] as u16) << 8) | buf[body + 1] as u16;
            if fcs16(&buf[..body]) != want {
                return Err(error_code::BAD_CHECKSUM);
            }
            if buf[0] & 0x7F != MSG_TYPE_SPDM {
                return Err(error_code::UNEXPECTED_MESSAGE_TYPE);
            }

            // buf[0] stays as the transport header; payload follows it.
            return Ok((SpdmPalIoKind::Message, body));
        }
    }

    /// Sends one SPDM message. `msg[0]` is filled in here; the caller has
    /// already written the payload into `msg[1..]`.
    async fn send_response(&mut self, kind: SpdmPalIoKind, msg: &mut [u8]) -> McuResult<()> {
        if msg.len() < HEADER_SIZE {
            return Err(error_code::BUFFER_TOO_SMALL);
        }
        if kind != SpdmPalIoKind::Message {
            return Err(error_code::OPERATION_NOT_SUPPORTED);
        }

        msg[0] = MSG_TYPE_SPDM;
        let fcs = fcs16(msg);

        self.send_byte(FLAG).await;
        for i in 0..msg.len() {
            let b = msg[i];
            self.send_escaped(b).await;
        }
        self.send_escaped((fcs >> 8) as u8).await;
        self.send_escaped((fcs & 0xFF) as u8).await;
        self.send_byte(FLAG).await;
        Ok(())
    }
}
'''

with open(TRANSPORT, "w") as f:
    f.write(TRANSPORT_RS)
print("added", TRANSPORT)

# ------------------------------------------------------------------ mod.rs
s = open(MOD).read()

old = "mod caliptra_vdm;"
new = """mod caliptra_vdm;
pub mod reg_transport;

/// Error codes for the register-channel transport.
pub mod reg_transport_errors {
    use caliptra_mcu_spdm_errors::SUBDOMAIN_MCTP;
    use mcu_error::{domain, McuErrorCode};

    pub const UNEXPECTED_MESSAGE_TYPE: McuErrorCode =
        McuErrorCode::new(domain::SPDM, SUBDOMAIN_MCTP, 0x0101);
    pub const BUFFER_TOO_SMALL: McuErrorCode =
        McuErrorCode::new(domain::SPDM, SUBDOMAIN_MCTP, 0x0102);
    pub const MESSAGE_TOO_LARGE: McuErrorCode =
        McuErrorCode::new(domain::SPDM, SUBDOMAIN_MCTP, 0x0103);
    pub const BAD_CHECKSUM: McuErrorCode =
        McuErrorCode::new(domain::SPDM, SUBDOMAIN_MCTP, 0x0104);
    pub const OPERATION_NOT_SUPPORTED: McuErrorCode =
        McuErrorCode::new(domain::SPDM, SUBDOMAIN_MCTP, 0x0105);
}"""
assert s.count(old) == 1, "mod anchor not unique"
s = s.replace(old, new, 1)

# spawn the new responder next to the MCTP one
old = """    if spawner.spawn(spdm_mctp_responder()).is_err() {"""
new = """    if spawner.spawn(spdm_reg_responder()).is_err() {
        // non-fatal: the MCTP responder below is the primary path
    }
    if spawner.spawn(spdm_mctp_responder()).is_err() {"""
assert s.count(old) == 1, "spawn anchor not unique"
s = s.replace(old, new, 1)

# the task itself, modelled on spdm_mctp_responder
old = """async fn spdm_mctp_responder() {"""
new = '''async fn spdm_reg_responder() {
    let mut cw = Console::<DefaultSyscalls>::writer();

    #[repr(C, align(64))]
    struct RegScratchBuf([u8; MCTP_SPDM_SCRATCH_SIZE]);
    static mut REG_SCRATCH: RegScratchBuf = RegScratchBuf([0u8; MCTP_SPDM_SCRATCH_SIZE]);
    // SAFETY: this task is the sole owner of `REG_SCRATCH`.
    let scratch_ptr: NonNull<u8> = unsafe { NonNull::new_unchecked(REG_SCRATCH.0.as_mut_ptr()) };

    // SAFETY: `init_once` is called once per task lifetime.
    static REG_ALLOC_CELL: StaticBitmapAllocatorCell = StaticBitmapAllocatorCell::new();
    let allocator: &'static BitmapAllocator =
        unsafe { REG_ALLOC_CELL.init_once(scratch_ptr, MCTP_SPDM_SCRATCH_SIZE) };

    let transport = alloc::boxed::Box::new(reg_transport::McuSpdmRegTransport::new());

    // SAFETY: `allocator` is the `&'static` handle obtained above and is
    // exclusive to this task.
    let pal = unsafe {
        McuSpdmPal::new(
            transport,
            allocator,
            crate::cert_store::shared(),
            measurement_provider(),
            MAX_INBOUND_SPDM_REQUEST_SIZE,
            MAX_BUFFERED_SPDM_MSG_SIZE,
        )
    };

    let mut stack = SpdmStack::<_, 1, _>::new(pal);

    crate::log_info!(cw, "SPDM_REG: starting spdm-lib run loop on the register channel");
    if let Err(e) = stack.run().await {
        crate::log_error!(
            cw,
            "SPDM_REG: run loop exited: 0x{}",
            crate::Hex32(u32::from(e))
        );
    }
}

#[embassy_executor::task]
async fn spdm_mctp_responder() {'''
assert s.count(old) == 1, "task anchor not unique"
s = s.replace(old, new, 1)

open(MOD, "w").write(s)
print("patched", MOD)

# ----------------------------------------------------------------- main.rs
s = open(MAIN).read()
if "const UART_ECHO_TEST: bool = true;" in s:
    s = s.replace("const UART_ECHO_TEST: bool = true;",
                  "const UART_ECHO_TEST: bool = false;  // stub off: the real "
                  "SPDM stack owns the register channel now")
    open(MAIN, "w").write(s)
    print(f"patched {MAIN} (stub disabled)")

print()
print("Build with:  cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes")
print()
print("IMPORTANT - the Pi's frames must change shape. Inside each 0x7E frame,")
print("spdm-lib expects ONE message-type byte (0x05) then the SPDM payload:")
print("    7E | 05 | 10 84 00 00 | FCS-hi | FCS-lo | 7E")
print("not the 4-byte MCTP transport header we send today. Use")
print("protocol.build_frame_raw() after patching the Pi side.")
