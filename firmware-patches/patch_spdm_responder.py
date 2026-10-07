#!/usr/bin/env python3
"""
patch_spdm_responder.py - make the MCU an SPDM responder with a return path.

Run from the root of caliptra-mcu-sw on the LAPTOP:
    python3 patch_spdm_responder.py

Replaces whatever uart_echo_loop currently does with a full responder:
receive a framed SPDM request, validate it, build the matching response, and
send it back to the host byte by byte.

Wire format (FPGA wrapper registers, both directions flow-controlled):

    0xa401012c  host -> MCU
        [7:0]   byte        [8]     valid
        [23:16] seq         [31:24] ack of the MCU's last byte

    0xa4010130  MCU -> host
        [7:0]   ack of the host's last byte
        [15:8]  byte        [23:16] seq
        [31:30] kept at 0b11, which MCU ROM reads at boot

Each side bumps its own sequence counter per byte and waits for the other to
echo that number back before sending the next. Nothing is lost regardless of
how either side is scheduled.

Pairs with vck190_agent.py on the VCK190, which bridges these registers to
/dev/ttyGS0 so the Pi can drive the whole thing over USB.

Undo with:
    git checkout platforms/emulator/runtime/userspace/apps/user/src/main.rs
"""
import re
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/main.rs"
s = open(P).read()

if "UART_ECHO_TEST" not in s:
    sys.exit("run patch_echo.py first (no UART_ECHO_TEST found)")
if "spdm-responder" in s:
    sys.exit("already patched")

NEW = r'''fn uart_echo_loop() -> ! {
    use caliptra_mcu_libsyscall_caliptra::DefaultSyscalls;
    use caliptra_mcu_libtock_console::Console;
    use caliptra_mcu_libtock_platform::Syscalls;
    type Con = Console<DefaultSyscalls>;

    // host -> MCU: [7:0] byte, [8] valid, [23:16] seq, [31:24] ack of our last byte
    const RX: *const u32 = 0xa401_012c as *const u32;
    // MCU -> host: [7:0] ack of their last byte, [15:8] byte, [23:16] seq
    const RET: *mut u32 = 0xa401_0130 as *mut u32;
    const RET_TOP: u32 = 0xC000_0000; // MCU ROM reads bits 29..31 at boot
    const FLAG: u8 = 0x7E;
    const ESCAPE: u8 = 0x7D;
    const POLLS_PER_YIELD: u32 = 64;
    const TX_ACK_LIMIT: u32 = 4_000_000; // give up on a byte rather than hang

    /// FCS-16 (RFC 1662), no lookup table.
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

    fn spdm_name(code: u8) -> &'static str {
        match code {
            0x84 => "GET_VERSION",
            0xE1 => "GET_CAPABILITIES",
            0xE3 => "NEGOTIATE_ALGORITHMS",
            0x81 => "GET_DIGESTS",
            0x82 => "GET_CERTIFICATE",
            0x83 => "CHALLENGE",
            0xE0 => "GET_MEASUREMENTS",
            0x04 => "VERSION",
            0x61 => "CAPABILITIES",
            0x01 => "DIGESTS",
            0x03 => "CHALLENGE_AUTH",
            _ => "UNKNOWN",
        }
    }

    /// The response this responder gives for a request code.
    fn response_for(code: u8) -> Option<(u8, &'static [u8])> {
        match code {
            0x84 => Some((0x04, &[0x00, 0x00, 0x01, 0x10])),       // VERSION
            0xE1 => Some((0x61, &[0x00, 0x00, 0x00, 0x01])),       // CAPABILITIES
            0xE3 => Some((0x63, &[0x00, 0x00, 0x01])),             // ALGORITHMS
            0x81 => Some((0x01, &[0x00, 0x01, 0xAB, 0xCD])),       // DIGESTS
            0x83 => Some((0x03, &[0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])), // CHALLENGE_AUTH
            _ => None,
        }
    }

    /// Build a framed response into `out`. Returns how many bytes were written.
    fn build_frame(code: u8, payload: &[u8], dest: u8, src: u8, tag: u8,
                   out: &mut [u8]) -> usize {
        let mut body = [0u8; 64];
        let mut n = 0;
        body[n] = 0x01; n += 1;                       // serial revision
        let len_at = n; body[n] = 0; n += 1;          // byte count, filled below
        let mctp_at = n;
        body[n] = 0x01; n += 1;                       // MCTP header version
        body[n] = dest; n += 1;
        body[n] = src; n += 1;
        body[n] = 0xC8 | (tag & 0x7); n += 1;         // SOM|EOM|TO|tag
        body[n] = 0x05; n += 1;                       // message type: SPDM
        body[n] = 0x10; n += 1;                       // SPDM 1.0
        body[n] = code; n += 1;
        for &b in payload {
            if n < body.len() - 2 { body[n] = b; n += 1; }
        }
        body[len_at] = (n - mctp_at) as u8;

        let fcs = fcs16(&body[..n]);
        body[n] = (fcs >> 8) as u8; n += 1;
        body[n] = (fcs & 0xFF) as u8; n += 1;

        // frame it: flags outside, escape 0x7E and 0x7D inside
        let mut w = 0;
        if w < out.len() { out[w] = FLAG; w += 1; }
        for &b in &body[..n] {
            if b == FLAG || b == ESCAPE {
                if w + 1 < out.len() {
                    out[w] = ESCAPE; w += 1;
                    out[w] = b ^ 0x20; w += 1;
                }
            } else if w < out.len() {
                out[w] = b; w += 1;
            }
        }
        if w < out.len() { out[w] = FLAG; w += 1; }
        w
    }

    /// Send bytes back to the host, waiting for each to be acknowledged.
    fn send_bytes(data: &[u8], tx_seq: &mut u32, ack_seq: u32) -> u32 {
        let mut sent = 0;
        for &b in data {
            *tx_seq = (*tx_seq + 1) & 0xFF;
            let word = RET_TOP | (*tx_seq << 16) | ((b as u32) << 8) | (ack_seq & 0xFF);
            unsafe { core::ptr::write_volatile(RET, word) };

            let mut spins: u32 = 0;
            loop {
                let v = unsafe { core::ptr::read_volatile(RX) };
                if ((v >> 24) & 0xFF) == *tx_seq {
                    sent += 1;
                    break;
                }
                spins += 1;
                if spins >= TX_ACK_LIMIT {
                    break; // host not reading; drop this byte rather than hang
                }
                if spins % POLLS_PER_YIELD == 0 {
                    DefaultSyscalls::yield_no_wait();
                }
            }
        }
        sent
    }

    let _ = Con::write(b"[spdm-responder] ready, waiting for requests\n");

    let mut last_rx_seq: u32 = 0xffff_ffff;
    let mut ack_seq: u32 = 0;
    let mut tx_seq: u32 = 0;
    let mut frame = [0u8; 160];
    let mut out = [0u8; 160];
    let mut n: usize = 0;
    let mut in_frame = false;
    let mut esc = false;
    let mut handled: u32 = 0;
    let mut polls: u32 = 0;

    loop {
        polls = polls.wrapping_add(1);
        if polls % POLLS_PER_YIELD == 0 {
            DefaultSyscalls::yield_no_wait();
        }

        let v = unsafe { core::ptr::read_volatile(RX) };
        if v & 0x100 == 0 {
            continue;
        }
        let seq = (v >> 16) & 0xff;
        if seq == last_rx_seq {
            continue;
        }
        last_rx_seq = seq;
        ack_seq = seq;
        // acknowledge immediately so the host can send the next byte
        unsafe {
            core::ptr::write_volatile(RET, RET_TOP | (tx_seq << 16) | (ack_seq & 0xFF))
        };

        let b = (v & 0xff) as u8;

        if b == FLAG {
            if in_frame && n > 4 {
                let rev = frame[0];
                let len = frame[1] as usize;
                let want = ((frame[n - 2] as u16) << 8) | frame[n - 1] as u16;
                let got = fcs16(&frame[..n - 2]);

                if rev != 0x01 || len != n - 4 {
                    let _ = writeln!(Con::writer(),
                        "[spdm-responder] bad header rev {:#04x} len {}", rev, len);
                } else if got != want {
                    let _ = writeln!(Con::writer(),
                        "[spdm-responder] bad FCS {:#06x} != {:#06x}", got, want);
                } else {
                    let dest = frame[3];
                    let src = frame[4];
                    let tag = frame[5] & 0x7;
                    let msg_type = if n > 6 { frame[6] } else { 0xFF };
                    let code = if n > 8 { frame[8] } else { 0xFF };
                    handled += 1;
                    let _ = writeln!(Con::writer(),
                        "[spdm-responder] rx {} {:#04x} {} (type {:#04x})",
                        handled, code, spdm_name(code), msg_type);

                    if let Some((rcode, payload)) = response_for(code) {
                        // reply goes back the way it came: swap EID direction
                        let w = build_frame(rcode, payload, src, dest, tag, &mut out);
                        let sent = send_bytes(&out[..w], &mut tx_seq, ack_seq);
                        let _ = writeln!(Con::writer(),
                            "[spdm-responder] tx {:#04x} {} ({}/{} bytes acked)",
                            rcode, spdm_name(rcode), sent, w);
                    } else {
                        let _ = writeln!(Con::writer(),
                            "[spdm-responder] no response defined for {:#04x}", code);
                    }
                }
            }
            in_frame = true;
            n = 0;
            esc = false;
        } else if !in_frame {
            // bytes outside a frame: ignore
        } else if esc {
            if n < frame.len() { frame[n] = b ^ 0x20; n += 1; }
            esc = false;
        } else if b == ESCAPE {
            esc = true;
        } else if n < frame.len() {
            frame[n] = b;
            n += 1;
        }
    }
}'''

m = re.search(r"fn uart_echo_loop\(\) -> ! \{.*?\n\}\n", s, re.S)
if not m:
    sys.exit("could not locate uart_echo_loop - paste main.rs so it can be adjusted")
s = s[: m.start()] + NEW + "\n" + s[m.end():]

open(P, "w").write(s)
print("patched", P)
print("note: the loop is still entered from async_main via UART_ECHO_TEST")
