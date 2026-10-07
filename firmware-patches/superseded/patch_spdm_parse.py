#!/usr/bin/env python3
"""
patch_spdm_parse.py - turn the echo loop into an SPDM frame parser.

Run from the root of caliptra-mcu-sw on the LAPTOP, after patch_echo_ack.py:
    python3 patch_spdm_parse.py

Instead of splitting incoming bytes on newlines, the loop now reassembles
DSP0253 serial frames (0x7E delimited, 0x7D escaping), checks the FCS-16,
decodes the MCTP header and SPDM request code, and prints one line per frame.
After EXPECTED_FRAMES good frames it prints a pass line.

Produces output like:

    [spdm-mock] frame 1 ok: MCTP type 0x05 SPDM 0x84 GET_VERSION
    [spdm-mock] frame 2 ok: MCTP type 0x05 SPDM 0xE1 GET_CAPABILITIES
    [spdm-mock] frame 3 ok: MCTP type 0x05 SPDM 0x81 GET_DIGESTS
    [spdm-mock] frame 4 ok: MCTP type 0x05 SPDM 0x83 CHALLENGE
    [spdm-mock] 4/4 frames verified - MOCK ATTESTATION PASSED

Bad frames report why (bad FCS / bad header / too short) rather than being
silently dropped, so a demo failure is diagnosable.

Undo with:
    git checkout platforms/emulator/runtime/userspace/apps/user/src/main.rs
"""
import sys

P = "platforms/emulator/runtime/userspace/apps/user/src/main.rs"
s = open(P).read()

if "ACK_TOP" not in s:
    sys.exit("run patch_echo_ack.py first")
if "spdm-mock" in s:
    sys.exit("already patched")

# ------------------------------------------------------------------ state
old = """    let mut last_seq: u32 = 0xffff_ffff;
    let mut total: u32 = 0;
    let mut line = [0u8; 96];
    let mut n: usize = 0;"""
new = """    // How many good frames make a pass.
    const EXPECTED_FRAMES: u32 = 4;
    const FLAG: u8 = 0x7E;
    const ESCAPE: u8 = 0x7D;

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

    fn spdm_name(code: u8) -> &'static str {
        match code {
            0x84 => "GET_VERSION",
            0xE1 => "GET_CAPABILITIES",
            0xE3 => "NEGOTIATE_ALGORITHMS",
            0x81 => "GET_DIGESTS",
            0x82 => "GET_CERTIFICATE",
            0x83 => "CHALLENGE",
            0xE0 => "GET_MEASUREMENTS",
            _ => "UNKNOWN",
        }
    }

    let mut last_seq: u32 = 0xffff_ffff;
    let mut total: u32 = 0;
    let mut frame = [0u8; 160];
    let mut n: usize = 0;
    let mut in_frame = false;
    let mut esc = false;
    let mut good: u32 = 0;
    let mut bad: u32 = 0;"""
assert s.count(old) == 1, "state anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------------------ banner text
old = 'let _ = Con::write(b"[uart-echo] direct-poll loop running\\n");'
new = 'let _ = Con::write(b"[spdm-mock] waiting for SPDM frames\\n");'
assert s.count(old) == 1, "banner anchor not unique"
s = s.replace(old, new)

# ------------------------------------------------------- frame state machine
old = """        let b = (v & 0xff) as u8;
        if b == b'\\n' || b == b'\\r' || n == line.len() {
            let text = core::str::from_utf8(&line[..n]).unwrap_or("<non-utf8>");
            let _ = writeln!(Con::writer(), "[uart-echo] rx#{} line: {:?}", total, text);
            n = 0;
        } else {
            line[n] = b;
            n += 1;
        }"""
new = """        let b = (v & 0xff) as u8;

        if b == FLAG {
            if in_frame && n > 0 {
                // End of a frame: validate, then report it.
                if n < 5 {
                    bad += 1;
                    let _ = writeln!(Con::writer(), "[spdm-mock] frame too short ({} bytes)", n);
                } else {
                    let rev = frame[0];
                    let len = frame[1] as usize;
                    let want = ((frame[n - 2] as u16) << 8) | frame[n - 1] as u16;
                    let got = fcs16(&frame[..n - 2]);

                    if rev != 0x01 || len != n - 4 {
                        bad += 1;
                        let _ = writeln!(
                            Con::writer(),
                            "[spdm-mock] bad header: rev {:#04x} len {} (frame {} bytes)",
                            rev, len, n
                        );
                    } else if got != want {
                        bad += 1;
                        let _ = writeln!(
                            Con::writer(),
                            "[spdm-mock] bad FCS: got {:#06x} want {:#06x}", got, want
                        );
                    } else {
                        // [2..6] MCTP header, [6] message type, [7] SPDM version, [8] code
                        let msg_type = if n > 6 { frame[6] } else { 0xFF };
                        let code = if n > 8 { frame[8] } else { 0xFF };
                        good += 1;
                        let _ = writeln!(
                            Con::writer(),
                            "[spdm-mock] frame {} ok: MCTP type {:#04x} SPDM {:#04x} {}",
                            good, msg_type, code, spdm_name(code)
                        );
                        if good == EXPECTED_FRAMES {
                            let _ = writeln!(
                                Con::writer(),
                                "[spdm-mock] {}/{} frames verified - MOCK ATTESTATION PASSED",
                                good, EXPECTED_FRAMES
                            );
                        }
                    }
                }
            }
            in_frame = true;
            n = 0;
            esc = false;
        } else if !in_frame {
            // bytes outside a frame: ignore
        } else if esc {
            if n < frame.len() {
                frame[n] = b ^ 0x20;
                n += 1;
            }
            esc = false;
        } else if b == ESCAPE {
            esc = true;
        } else if n < frame.len() {
            frame[n] = b;
            n += 1;
        }

        let _ = total;
        let _ = bad;"""
assert s.count(old) == 1, "loop-body anchor not unique"
s = s.replace(old, new)

open(P, "w").write(s)
print("patched", P)
