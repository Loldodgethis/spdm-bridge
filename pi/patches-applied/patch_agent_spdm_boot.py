#!/usr/bin/env python3
"""
patch_agent_spdm_boot.py - make !BOOT start firmware that runs the REAL SPDM stack.

Run on the VCK190:
    python3 patch_agent_spdm_boot.py
    systemctl restart vck190-agent

Why
---
The agent's boot command used `--profile=nightly` with a `test(=...)` filter.
Two things went wrong with that:

  * the SPDM tests are marked #[ignore], so nextest skipped them unless
    `--run-ignored all` is passed
  * without SPDM_VALIDATOR_DIR the tests exit in milliseconds, before any
    firmware boots

This switches to exactly what CI uses for FPGA SPDM runs:

    --profile=nightly-ci-spdm --run-ignored all --no-fail-fast
    SPDM_VALIDATOR_DIR=<validator>

That profile's own default-filter selects the attestation and conformance
tests, so no -E expression is needed. Those tests still FAIL at the end (the
I3C/MCTP side times out, and the conformance validator binary is missing) but
they boot the SPDM-enabled app first, which is what the register channel needs:

    SPDM_REG: starting spdm-lib register-channel run loop

The agent reports !READY as soon as the MCU answers, so the Pi can send
requests during the window the test is up.
"""
import os
import re
import sys

P = "/root/vck190_agent.py"
try:
    s = open(P).read()
except OSError:
    sys.exit(f"{P} not found - run this on the VCK190")

if "nightly-ci-spdm" in s:
    sys.exit("already patched")

old = re.search(r'BOOT_CMD = \((?:.|\n)*?\)\n', s)
if not old:
    sys.exit("could not find BOOT_CMD in the agent")

new = '''BOOT_CMD = (
    "cd {repo} && sudo SPDM_VALIDATOR_DIR={validator} "
    "CPTRA_FIRMWARE_BUNDLE=$HOME/all-fw.zip cargo-nextest nextest run "
    "--workspace-remap=. --archive-file $HOME/caliptra-test-binaries.tar.zst "
    "--no-capture --no-fail-fast --profile=nightly-ci-spdm --run-ignored all"
)
'''
s = s[: old.start()] + new + s[old.end():]

# the test name is no longer part of the command; the profile filter picks them
s = s.replace(
    '        self.cmd = BOOT_CMD.format(repo=repo, test=test)',
    '        self.cmd = BOOT_CMD.format(repo=repo, validator=validator)')
s = s.replace(
    '    def __init__(self, repo, test):',
    '    def __init__(self, repo, test=None, validator="/root/spdm-emu/build/bin"):')
s = s.replace(
    '    fw = Firmware(args.repo, args.test)',
    '    fw = Firmware(args.repo, args.test, args.validator)')
s = s.replace(
    '    ap.add_argument("--test", default=DEFAULT_TEST)',
    '    ap.add_argument("--test", default=DEFAULT_TEST,\n'
    '                    help="unused with the SPDM profile; kept for compatibility")\n'
    '    ap.add_argument("--validator", default="/root/spdm-emu/build/bin",\n'
    '                    help="SPDM_VALIDATOR_DIR for the boot command")')

open(P, "w").write(s)
print("patched", P)

if not os.path.isdir("/root/spdm-emu/build/bin"):
    print("WARNING: /root/spdm-emu/build/bin not found - pass --validator <dir>")

print()
print("restart the agent:   systemctl restart vck190-agent")
print("then press Boot + Run in the GUI; the boot takes ~2 minutes because the")
print("SPDM app image is much larger than the loopback one.")
