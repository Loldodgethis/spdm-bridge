#!/usr/bin/env python3
"""
patch_fix_reg_task.py - rebuild the register responder task from the repo's own
MCTP task, so it matches this tree's API exactly.

Run from the root of caliptra-mcu-sw on the LAPTOP, after
patch_reg_spdm_transport.py:
    python3 patch_fix_reg_task.py

Why
---
The first patch hand-wrote the task against an API that differs between
revisions of this repo: McuSpdmPal::new takes a different number of
arguments, the scratch/size constants are named differently, and
SUBDOMAIN_MCTP moved crates.

Rather than guess, this copies `spdm_mctp_responder` out of your own
spdm/mod.rs and rewrites it into `spdm_reg_responder`, changing only:

  * the transport            -> McuSpdmRegTransport::new()
  * the statics              -> distinct names so the two tasks don't collide
  * the readiness signal     -> dropped (MCI's flag is MCTP-specific)
  * the log tags             -> SPDM_REG

Everything else, including the exact McuSpdmPal::new call and the stack
construction, is taken verbatim from the working MCTP task.
"""
import re
import sys

MOD = "platforms/emulator/runtime/userspace/apps/user/src/spdm/mod.rs"
try:
    s = open(MOD).read()
except OSError:
    sys.exit("run from the root of caliptra-mcu-sw")

if "McuSpdmRegTransport" not in s:
    sys.exit("run patch_reg_spdm_transport.py first")


def extract_fn(text, name):
    """Return (start, end) spanning `async fn <name>() { ... }` by brace match."""
    m = re.search(rf"async fn {name}\(\)\s*\{{", text)
    if not m:
        return None
    i = text.index("{", m.start())
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return (m.start(), j + 1)
    return None


# ---------------------------------------------- 1. drop the hand-written task
span = extract_fn(s, "spdm_reg_responder")
if span:
    start, end = span
    # also remove the #[embassy_executor::task] attribute above it
    head = s[:start].rstrip()
    if head.endswith("#[embassy_executor::task]"):
        head = head[: -len("#[embassy_executor::task]")].rstrip()
    s = head + "\n\n" + s[end:].lstrip("\n")
    print("removed the hand-written spdm_reg_responder")

# ---------------------------------------------- 2. copy the working MCTP task
span = extract_fn(s, "spdm_mctp_responder")
if not span:
    sys.exit("could not find spdm_mctp_responder in spdm/mod.rs")
start, end = span
mctp_task = s[start:end]

reg_task = mctp_task

# transport: swap the MCTP construction for ours, however it is written
reg_task = re.sub(
    r"let transport = alloc::boxed::Box::new\(\s*McuSpdmMctpTransport::new\(.*?\)\s*,?\s*\);",
    "let transport = alloc::boxed::Box::new(reg_transport::McuSpdmRegTransport::new());",
    reg_task,
    flags=re.S,
)
if "McuSpdmRegTransport" not in reg_task:
    sys.exit("could not rewrite the transport construction - paste "
             "spdm_mctp_responder so it can be matched")

# Drop the MCI readiness flag FIRST: it is MCTP-specific, and renaming before
# removing would change the method name out from under this match.
reg_task = re.sub(
    r"\n\s*Mci::<DefaultSyscalls>::new\(\)\s*\n?\s*\.set_spdm_mctp_responder_ready\(\)\s*\n?\s*\.unwrap\(\);",
    "",
    reg_task,
)

# Rename identifiers with word boundaries, each applied once to the original
# text, so no replacement feeds into another.
renames = {
    r"\bspdm_mctp_responder\b": "spdm_reg_responder",
    r"\bMCTP_SCRATCH\b": "REG_SCRATCH",
    r"\bMCTP_ALLOC_CELL\b": "REG_ALLOC_CELL",
    r"\bScratchBuf\b": "RegScratchBuf",
    r"SPDM_MCTP:": "SPDM_REG:",
    r"MCTP run loop": "register-channel run loop",
    r"this is the\n    // MCTP responder task": "this is the\n    // register-channel responder task",
}
for pat, rep in renames.items():
    reg_task = re.sub(pat, rep, reg_task)

# VDM backend is wired for MCTP; keep it only if it referenced nothing MCTP-only
s = s[:start] + reg_task + "\n\n#[embassy_executor::task]\n" + mctp_task + s[end:]
print("rebuilt spdm_reg_responder from spdm_mctp_responder")

# ------------------------------------------------- 3. fix the errors module
s = s.replace("use caliptra_mcu_spdm_errors::SUBDOMAIN_MCTP;",
              "use caliptra_mcu_spdm_traits::SUBDOMAIN_MCTP;")

open(MOD, "w").write(s)
print("patched", MOD)
print()
print("rebuild with:")
print("  cargo xtask-fpga fpga build --target-host root@<ip> --separate-runtimes")
print()
print("if SUBDOMAIN_MCTP is not in the traits crate either, run:")
print("  grep -rn 'SUBDOMAIN_MCTP' --include=*.rs runtime/userspace/api/spdm-lib | head")
print("and tell me which crate exports it.")
