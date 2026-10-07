# SPDM Bridge

Python framework running on the Raspberry Pi. Links the Caliptra FPGA (VCK190)
and the Tektagon board, decodes SPDM/MCTP traffic in both directions, and shows
it live in a browser.

```
  Tektagon  --I2C--  Raspberry Pi  --USB--  VCK190 (ARM Linux)
                          |                      |
                    this framework          vck190_agent.py
                          |                      |
                 web GUI :8080            FPGA registers -> Caliptra MCU
```

Roles: the Pi is **responder** to the Tektagon and **requestor** to the FPGA.

## Run it

Mock mode needs nothing but Python:

```bash
python3 -m spdm_bridge                                  # both sides mocked
python3 -m spdm_bridge --auto                           # run a sequence at startup
python3 -m spdm_bridge --fpga serial:/dev/ttyACM0       # real FPGA over USB
python3 -m spdm_bridge --tektagon i2c:0x38              # Pi as I2C master
python3 -m spdm_bridge --tektagon bsc:0x38              # Pi as I2C slave
python3 -m spdm_bridge --mode local                     # Pi answers by itself
python3 -m spdm_bridge --raw-log ~/uart.log             # keep a raw byte log
```

Typical working setup:

```bash
python3 -m spdm_bridge --fpga serial:/dev/ttyACM0 --tektagon mock --raw-log ~/uart.log
```

Then open `http://<pi>:8080` from a laptop browser.

Real transports need extras on the Pi: `pip install pyserial smbus2`, and
`sudo apt install pigpio python3-pigpio` for the slave transport.

## Windows

| URL | Shows |
| --- | --- |
| `/` | decoded SPDM traffic, FPGA and Tektagon side by side, plus the controls |
| `/uart` | raw byte stream in and out of each endpoint, hex and ASCII |

"Open UART window" on the main page opens the second one, so the decoded view
and the byte stream can sit on separate screens. A local ring buffer (8000
chunks) backs both, so the UART window shows recent history when opened.

## Buttons

| Button | Does |
| --- | --- |
| Boot + Run | asks the agent for firmware, waits for it, sends the SPDM sequence |
| Run SPDM sequence | sends the four requests without booting |
| Boot firmware / Stop firmware | `!BOOT` / `!STOP` to the agent |
| Status | `!STATUS` - replies `!UP` or `!DOWN` |
| GET_VERSION ... CHALLENGE | send one request |

## Transports

| Spec | Role | Use |
| --- | --- | --- |
| `mock` | - | fake board that answers SPDM requests; no hardware |
| `mock:noreply` | - | fake board that stays silent |
| `serial:/dev/ttyACM0` | - | USB link to the VCK190 agent |
| `ssh:root@<ip>` | - | runs uart_tx.py over the network; write-only |
| `i2c:0x38[:bus]` | master | Pi drives the bus; auto-detects the read pattern |
| `bsc:0x38` | slave | Pi is addressed by a master, on GPIO 18/19 |

## Responder modes (`--mode`)

| Mode | A request from the Tektagon is |
| --- | --- |
| `proxy` (default) | forwarded to the FPGA; the MCU's real answer goes back |
| `local` | answered by the Pi itself |
| `observe` | logged only |

## On the VCK190

`vck190_agent.py` runs as a systemd service and joins the USB gadget serial
port to the FPGA registers, so the MCU can be reached. It also accepts control
lines over the same link: `!BOOT`, `!STOP`, `!STATUS`, `!PING`, and answers
`!BOOTING`, `!READY`, `!UP`, `!DOWN`, `!BUSY`, `!EXIT <code>`.

After a reboot of the VCK190: re-bootstrap the FPGA, and recreate the USB
gadget if `/dev/ttyGS0` is missing. The agent service itself comes back on its
own.

## Files

| File | What it does |
| --- | --- |
| `protocol.py` | framing, FCS-16, MCTP and SPDM decode, canned request/response set |
| `transports.py` | one class per link, all swappable |
| `bridge.py` | polls both endpoints, decodes, relays, keeps the logs |
| `server.py` | both web pages and the JSON API |
| `__main__.py` | CLI entry point |

## API

```
GET  /api/status            endpoint status and counters
GET  /api/log?since=N       decoded messages
GET  /api/raw?since=N       raw byte chunks
POST /api/send              {"target":"fpga","code":132} or {"hex":"7E 01 ..."}
POST /api/sequence          send the canned SPDM exchange
POST /api/control           {"target":"fpga","text":"!BOOT"}
POST /api/run               boot if needed, then send the sequence
```

## Known gaps

- **Tektagon link unproven.** Nothing has yet sent the Pi a byte over I2C. If
  the Tektagon answers reads, `i2c:` works as is; if it drives the bus, its
  SDA/SCL must move to GPIO 18 (pin 12) and GPIO 19 (pin 35) for `bsc:`.
  The Pi's main controller is master-only in hardware.
- **MCU lifetime.** Firmware lives about 20 seconds per boot, which fits four
  requests. Longer exchanges need a longer-running test.
- **Mock responses carry no real crypto.** They are structurally valid SPDM,
  nothing more.
