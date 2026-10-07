# caliptra-spdm-bridge

SPDM attestation tooling for a Caliptra subsystem on a VCK190 FPGA, driven
from a Raspberry Pi.

```
  Tektagon / XO5D  --I2C--  Raspberry Pi  --USB--  VCK190 (ARM Linux)
                                 |                      |
                           spdm_bridge             vck190_agent.py
                        (requestor + GUI)                |
                                 |              FPGA wrapper registers
                           browser :8080                 |
                                                  Caliptra MCU
                                                (real spdm-lib responder)
```

The Pi is the SPDM **requestor** to the FPGA and the **responder** to the
Tektagon. The FPGA half works on hardware today; the Tektagon half is
written but unproven, pending details of that board's I2C behaviour.

## Why a register channel and not a UART

The Caliptra MCU has no UART peripheral on this bitstream. The device tree
shows only the two Versal PS UARTs, which belong to ARM Linux, and the MCU's
`read_byte()` was a stub with no inbound FIFO in the FPGA wrapper. Firmware
output went one way into a debug FIFO and nothing came back.

So bytes cross into the MCU through two wrapper registers instead, with a
sequence-number handshake in each direction:

```
0xa401012c  host -> MCU   [7:0] byte  [8] valid  [23:16] seq  [31:24] ack of MCU's last byte
0xa4010130  MCU -> host   [7:0] ack   [15:8] byte [23:16] seq
```

On top of that sits DSP0253-style framing (`0x7E` delimiters, `0x7D`
escaping, FCS-16) carrying one message-type byte plus the SPDM message,
which is what spdm-lib's transport layer expects.

## Layout

| Path | What it is | Runs on |
| --- | --- | --- |
| `pi/spdm_bridge/` | the framework: SPDM requestor, transports, web GUI | Raspberry Pi |
| `vck190/vck190_agent.py` | byte pump between the USB gadget serial port and the registers, plus a `!BOOT` control channel | VCK190 |
| `vck190/vck190-agent.service` | systemd unit for the agent | VCK190 |
| `vck190/uart_tx.py` | standalone register writer, useful for poking the channel by hand | VCK190 |
| `firmware-patches/` | patches applied to `caliptra-mcu-sw` to add the SPDM transport | laptop (build host) |
| `tools/` | frame generator, I2C probe, I2C slave listener, standalone packet library | anywhere |
| `tests/` | pytest suites for the packet library and the FPGA registers | anywhere / VCK190 |
| `docs/` | setup, protocol notes, current status | — |

## Quick start

See `docs/SETUP.md` for the full three-machine setup. In short:

```bash
# Pi
python3 -m spdm_bridge --fpga serial:/dev/ttyACM0 --tektagon mock --frame raw

# VCK190
systemctl start vck190-agent

# then open http://<pi>:8080 and press "Run attestation"
```

## Status

Confirmed working on hardware:

- the register channel, both directions, with flow control and no byte loss
- Caliptra's real SPDM responder running on it (`SPDM_REG` in the MCU log)
- `GET_VERSION` -> `VERSION`: the device offers **SPDM 1.3 and 1.2**
- `GET_CAPABILITIES` -> `CAPABILITIES`, flags `0x00060016` =
  **CERT, CHAL, MEAS_SIG, CHUNK, ALIAS_CERT**, DataTransferSize 1023,
  MaxSPDMmsgSize 8192

Not yet done:

- the rest of the flow past CAPABILITIES (algorithms, digests, certificate,
  challenge, measurements) - the code is written, the bodies need confirming
  against the responder
- certificate reassembly across chunks
- the Tektagon/XO5D link: address, bus role and framing all unconfirmed

See `docs/STATUS.md` for detail.
