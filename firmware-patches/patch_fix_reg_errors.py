#!/usr/bin/env python3
"""
patch_fix_reg_errors.py - stop defining new error codes; reuse the ones the
app already has access to.

Run from the root of caliptra-mcu-sw on the LAPTOP:
    python3 patch_fix_reg_errors.py

Why
---
`SUBDOMAIN_MCTP` lives in the `caliptra-mcu-spdm-errors` crate, which the user
app does not depend on, so defining fresh McuErrorCode constants in the app
cannot work without adding a dependency.

The app DOES depend on `caliptra-mcu-spdm-transports`, which already exports
finished error constants for exactly this situation in
`errors::mctp`. This re-exports those under the names reg_transport.rs uses:

    UNEXPECTED_MESSAGE_TYPE -> errors::mctp::UNEXPECTED_MESSAGE_TYPE
    BUFFER_TOO_SMALL        -> errors::mctp::BUFFER_TOO_SMALL
    OPERATION_NOT_SUPPORTED -> errors::mctp::OPERATION_NOT_SUPPORTED
    MESSAGE_TOO_LARGE       -> errors::mctp::INVALID_MESSAGE
    BAD_CHECKSUM            -> errors::mctp::INVALID_MESSAGE

The last two collapse onto INVALID_MESSAGE, which is what a malformed frame
is from the stack's point of view. The distinction only mattered for our own
logging, and the transport can log the detail itself if needed.
"""
import re
import sys

MOD = "platforms/emulator/runtime/userspace/apps/user/src/spdm/mod.rs"
try:
    s = open(MOD).read()
except OSError:
    sys.exit("run from the root of caliptra-mcu-sw")

if "pub use caliptra_mcu_spdm_transports::errors::mctp" in s:
    sys.exit("already patched")

# Replace the whole hand-rolled error module with re-exports.
pattern = re.compile(
    r"/// Error codes for the register-channel transport\.\s*"
    r"pub mod reg_transport_errors \{.*?\n\}", re.S)

replacement = '''/// Error codes for the register-channel transport.
///
/// These come from the transports crate rather than being defined here: the
/// app depends on `caliptra-mcu-spdm-transports` but not on the lower-level
/// errors crate that owns the subdomain constants.
pub mod reg_transport_errors {
    pub use caliptra_mcu_spdm_transports::errors::mctp::{
        BUFFER_TOO_SMALL, OPERATION_NOT_SUPPORTED, UNEXPECTED_MESSAGE_TYPE,
    };

    /// A frame longer than the receive buffer: malformed as far as the
    /// stack is concerned.
    pub use caliptra_mcu_spdm_transports::errors::mctp::INVALID_MESSAGE as MESSAGE_TOO_LARGE;
    /// A frame whose FCS did not match: likewise malformed.
    pub use caliptra_mcu_spdm_transports::errors::mctp::INVALID_MESSAGE as BAD_CHECKSUM;
}'''

if not pattern.search(s):
    sys.exit("could not find the reg_transport_errors module - "
             "paste spdm/mod.rs lines 10-35 so it can be matched")

s = pattern.sub(replacement, s, count=1)
open(MOD, "w").write(s)
print("patched", MOD)
print()
print("rebuild with:")
print("  cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes")
