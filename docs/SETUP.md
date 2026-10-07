# Setup

Three machines, each with a distinct job.

| Machine | Job |
| --- | --- |
| Laptop (WSL) | builds firmware; only needed when firmware source changes |
| VCK190 | runs the agent and the MCU firmware |
| Raspberry Pi | runs the framework and serves the GUI |

## Laptop: build the firmware

Needs the `caliptra-mcu-sw` repo, Rust, and Docker or Podman.

```bash
cd ~/caliptra-mcu-sw

# apply the firmware patches, in this order
python3 firmware-patches/patch_spdm_responder.py        # stub responder (superseded, sets the hook)
python3 firmware-patches/patch_reg_spdm_transport.py    # the real transport + responder task
python3 firmware-patches/patch_fix_reg_task.py          # rebuild the task from this tree's API
python3 firmware-patches/patch_fix_reg_errors.py        # reuse the transports crate's error codes
python3 firmware-patches/patch_reg_transport_resilient.py  # skip bad frames, don't kill the stack
python3 firmware-patches/patch_reg_transport_debug.py   # logging + bounded ack wait

cargo xtask-fpga fpga bootstrap --target-host root@<vck190> --configuration subsystem
cargo xtask-fpga fpga build      --target-host root@<vck190> --separate-runtimes
cargo xtask-fpga fpga build-test --target-host root@<vck190>
```

`bootstrap` is needed again after any reboot or power cycle of the board.

## VCK190: the agent

```bash
scp vck190/vck190_agent.py vck190/vck190-agent.service vck190/uart_tx.py root@<vck190>:/root/

# on the board
cp /root/vck190-agent.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vck190-agent
systemctl is-active vck190-agent
```

The unit sets `Environment=HOME=/root`; without it `$HOME` is empty under
systemd and the boot command cannot find the firmware bundle.

The USB gadget must exist (`/dev/ttyGS0`). It is configured through configfs
and does not survive a reboot unless scripted.

## Pi: the framework

```bash
scp -r pi/spdm_bridge pi@<pi>:~/
ssh pi@<pi>
pip install pyserial smbus2          # only for the real transports
python3 -m spdm_bridge --fpga serial:/dev/ttyACM0 --tektagon mock --frame raw --raw-log ~/uart.log
```

Open `http://<pi>:8080`. "Open UART window" gives the raw byte stream in a
second window.

## Running an attestation

The MCU only exists while a test is running on the VCK190, and the responder
appears about two minutes into it.

```bash
# VCK190, terminal 1
cd ~/caliptra-mcu-sw
sudo SPDM_VALIDATOR_DIR=/root/spdm-emu/build/bin CPTRA_FIRMWARE_BUNDLE=$HOME/all-fw.zip \
  cargo-nextest nextest run --workspace-remap=. \
  --archive-file $HOME/caliptra-test-binaries.tar.zst \
  --profile=nightly-ci-spdm --run-ignored all --no-fail-fast --no-capture 2>&1 | tee /tmp/live.log

# laptop, terminal 2: waits for the responder, then fires
ssh root@<vck190> 'tail -n0 -F /tmp/live.log | grep -m1 "SPDM_REG: starting"' && \
curl -s -X POST http://<pi>:8080/api/attest -H 'Content-Type: application/json' -d '{"target":"fpga"}'
```

Three flags matter in that command and each cost an afternoon to find:
`--run-ignored all` because the SPDM tests are `#[ignore]`,
`--profile=nightly-ci-spdm` because that profile's filter selects them, and
`SPDM_VALIDATOR_DIR` because the tests skip instantly without it.

## The validator

`SPDM_VALIDATOR_DIR` points at a build of `chipsalliance/caliptra-spdm-emu`.
It must be built **on the VCK190** (aarch64); an x86-64 build from the laptop
will not run there.

```bash
cd /root && git clone --recursive https://github.com/chipsalliance/caliptra-spdm-emu.git spdm-emu
cd spdm-emu/build
cmake -DARCH=aarch64 -DTOOLCHAIN=GCC -DTARGET=Debug -DCRYPTO=openssl \
      -DCMAKE_C_FLAGS="-DLIBSPDM_MAX_CERT_CHAIN_SIZE=0x10000" ..
make copy_sample_key && make -j2
```

`spdm_requester_emu` is the binary the attestation tests use;
`spdm_device_validator_sample` fails to build on the `caliptra-main` branch
against its pinned libspdm, and only the conformance test needs it.
