# Wire protocol

## Registers

Both in the FPGA wrapper, which is the first map of `/dev/uio0` on the
VCK190's ARM side and user-accessible MMIO from the MCU.

```
0xa401012c   host -> MCU
    [7:0]    byte
    [8]      valid
    [23:16]  sequence number, incremented per byte
    [31:24]  acknowledgement of the MCU's last byte

0xa4010130   MCU -> host
    [7:0]    acknowledgement of the host's last byte
    [15:8]   byte
    [23:16]  sequence number
    [31:30]  kept at 0b11; MCU ROM reads these at boot
```

Each side bumps its own sequence number per byte and waits for the other to
echo it back. Nothing is lost regardless of scheduling. The MCU's wait is
bounded, so a host that stops reading cannot wedge the responder.

## Framing

```
0x7E | 0x05 | SPDM message | FCS-hi | FCS-lo | 0x7E
       ^ message type byte, what spdm-lib calls header_size() == 1
```

`0x7E` and `0x7D` inside the frame are escaped as `0x7D 0x5E` and
`0x7D 0x5D`. FCS-16 is RFC 1662, covering everything between the flags.

Note this is **not** the 4-byte MCTP transport header. spdm-lib's transport
layer wants one message-type byte; endpoint and tag are the driver's job.
The MCTP form is still supported by `protocol.build_frame()` for other links.

## Observed responses

```
GET_VERSION       7E 05 10 84 00 00 45 0F 7E
VERSION           7E 05 10 04 00 00 00 02 00 13 00 12 A6 4D 7E
                                    ^count 2  ^1.3  ^1.2

GET_CAPABILITIES  7E 05 13 E1 00 00 00 0C 00 00 00 00 00 00 00 04 00 00 00 04 00 00 ...
CAPABILITIES      7E 05 13 61 00 00 00 14 00 00 16 00 06 00 FF 03 00 00 00 20 00 00 C0 11 7E
                                       ^CT=20   ^flags 0x00060016  ^1023   ^8192
```

Flags `0x00060016` = CERT | CHAL | MEAS_SIG | CHUNK | ALIAS_CERT, per
spdm-lib's `CapFlags` in `codec/src/capabilities.rs`.

## Control channel

Lines outside frames, starting with `!`, are for the agent rather than the
MCU, so they cannot collide with SPDM traffic.

| Sent | Meaning | Agent replies |
| --- | --- | --- |
| `!BOOT` | start firmware locally | `!BOOTING`, later `!READY` |
| `!STATUS` | is the MCU answering | `!UP` or `!DOWN` |
| `!STOP` | kill the running test | `!STOPPED` or `!IDLE` |
| `!PING` | liveness | `!PONG` |
