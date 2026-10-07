# Status

## Working on hardware

- **Register channel**, both directions, sequence-number handshake, no loss
  measured over thousands of bytes.
- **USB transport**: Pi `/dev/ttyACM0` to VCK190 `/dev/ttyGS0`, agent bridges
  to the registers.
- **Real SPDM responder** on the channel: `SPDM_REG: starting spdm-lib
  register-channel run loop` in the MCU log, with spdm-lib handling the
  messages, not a stub.
- **GET_VERSION -> VERSION**: offers SPDM 1.3 and 1.2.
- **GET_CAPABILITIES -> CAPABILITIES**: CERT, CHAL, MEAS_SIG, CHUNK,
  ALIAS_CERT; CTExponent 20; DataTransferSize 1023; MaxSPDMmsgSize 8192.
- **GUI**: decoded SPDM in one window, raw bytes in another, local ring
  buffer behind both, one-click boot and run.

## Not done

- **The flow past CAPABILITIES.** NEGOTIATE_ALGORITHMS, GET_DIGESTS,
  GET_CERTIFICATE, CHALLENGE, GET_MEASUREMENTS are implemented in
  `protocol.py` but the request bodies are not yet confirmed against the
  responder. `req_negotiate_algorithms()` is the least certain.
- **Certificate reassembly.** DataTransferSize is 1023 and a chain is larger,
  so GET_CERTIFICATE needs fetching by offset and reassembling. CHUNK is
  advertised, so the responder supports it.
- **The Tektagon / XO5D link.** Three unknowns block it: the board's I2C
  address, whether it is bus master or answers reads, and whether it uses
  MCTP over I2C (DSP0237). Both transports are written: `i2c:` for Pi as
  master, `bsc:` for Pi as slave on GPIO 18/19.

## Environment problems seen, not caused by this work

- `test_mctp_spdm_attestation` fails at `IMAGE_LOADER_APP: image_loading
  failed`, and `test_mctp_spdm_attestation_pcr_quote` fails with
  `MCTP_UTIL: timed out waiting for IBI`. Both run on the I3C/MCTP path, not
  the register channel.
- Caliptra's `IDEP` (IDevID provisioning) mailbox command takes roughly 800M
  cycles and emits many `Timeout waiting for EXECUTE bit to clear` from the
  hw-model. Unclear whether that is expected on FPGA.
- The MCU only lives while a test runs, and these tests fail a minute or two
  after the responder appears, which makes for a narrow working window. A
  test that boots the SPDM app and simply waits would remove this.

## Lessons that cost time

- The SPDM tests are `#[ignore]`; without `--run-ignored all` nextest reports
  "no tests to run" while still listing them as skipped.
- They also skip instantly unless `SPDM_VALIDATOR_DIR` is set.
- Returning `Err` from the transport's `recv_request` ends spdm-lib's run
  loop for the whole boot. Bad frames must be skipped, not reported.
- An unbounded wait for a byte acknowledgement wedges the responder inside
  `send_response` if the host stops reading.
- GET_VERSION must always go out at version 1.0; later requests use whatever
  the responder offered.
