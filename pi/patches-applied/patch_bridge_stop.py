#!/usr/bin/env python3
"""
patch_bridge_stop.py - add a Stop button and make Boot + Run handle !BUSY.

Run on the Pi, next to the spdm_bridge/ directory, after patch_bridge_boot.py:
    python3 patch_bridge_stop.py

Adds:
  * "Stop firmware" button, sending !STOP to the agent
  * Boot + Run now copes with a test already running: if the agent answers
    !BUSY it waits for the current run to end rather than giving up, and if
    the MCU is already up it skips booting and sends straight away

Restart the framework afterwards.
"""
import sys

S = "spdm_bridge/server.py"
try:
    s = open(S).read()
except OSError:
    sys.exit("run this next to the spdm_bridge/ directory")

if "Stop firmware" in s:
    sys.exit("already patched")

# ------------------------------------------------------------------- button
old = """  <button onclick="ctl('!STATUS')">Status</button>"""
new = """  <button onclick="ctl('!STOP')">Stop firmware</button>
  <button onclick="ctl('!STATUS')">Status</button>"""
assert s.count(old) == 1, "button anchor not unique"
s = s.replace(old, new)

# --------------------------------------------------- busy-aware boot-and-run
old = '''                def boot_and_run():
                    # firmware only lives while a test runs, so ask for one,
                    # wait until the MCU answers, then send the requests
                    if bridge.last_control.get(target) != "!READY":
                        bridge.control(target, "!BOOT")
                        if not bridge.wait_for(target, "!READY", 150):
                            bridge.note("firmware did not come up in time",
                                        kind="error")
                            return
                    bridge.send(target, protocol.canned_request_stream())'''
new = '''                def boot_and_run():
                    import time as _t

                    # Firmware only lives while a test runs. Ask the agent
                    # what state it is in, then do the least work needed.
                    bridge.control(target, "!STATUS")
                    _t.sleep(1.5)
                    state = bridge.last_control.get(target)

                    if state != "!UP":
                        bridge.control(target, "!BOOT")
                        _t.sleep(1.0)
                        if bridge.last_control.get(target) == "!BUSY":
                            # a previous run is still going; let it finish
                            bridge.note("a test is already running - waiting",
                                        kind="info")
                            bridge.wait_for(target, "!DOWN", 90)
                            bridge.control(target, "!BOOT")
                        if not bridge.wait_for(target, "!READY", 150):
                            bridge.note("firmware did not come up in time",
                                        kind="error")
                            return

                    bridge.send(target, protocol.canned_request_stream())'''
assert s.count(old) == 1, "boot_and_run anchor not unique"
s = s.replace(old, new)

open(S, "w").write(s)
print("patched", S)
print("restart the framework to pick this up")
