"""
Entry point.

    python3 -m spdm_bridge                                  # both sides mocked
    python3 -m spdm_bridge --fpga ssh:root@172.31.1.116     # real FPGA link
    python3 -m spdm_bridge --tektagon i2c:0x38              # real Tektagon link
    python3 -m spdm_bridge --no-relay                       # observe only

Then open http://<pi-address>:8080 from a laptop browser.
"""
import argparse
import time

from . import protocol, transports
from .bridge import Bridge
from .server import serve


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fpga", default="mock",
                    help="mock | serial:/dev/ttyUSB1 | ssh:root@<ip> (default: mock)")
    ap.add_argument("--tektagon", default="mock",
                    help="mock | i2c:0x38[:bus] | serial:/dev/ttyUSB0 (default: mock)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-relay", action="store_true",
                    help="log both sides but do not forward between them")
    ap.add_argument("--frame", choices=["mctp", "raw"], default="mctp",
                    help="frame shape on the FPGA link: 'mctp' keeps the 4-byte "
                         "MCTP header, 'raw' sends the 1-byte message-type "
                         "header spdm-lib expects")
    ap.add_argument("--mode", choices=["proxy", "local", "observe"],
                    default="proxy",
                    help="how the Pi answers Tektagon requests: forward them to "
                         "the FPGA (proxy), answer here (local), or just log "
                         "them (observe)")
    ap.add_argument("--raw-log", help="also append the raw byte stream to this file")
    ap.add_argument("--auto", action="store_true",
                    help="send the canned SPDM sequence once at startup")
    args = ap.parse_args()

    fpga = transports.build(args.fpga, "fpga")
    tek = transports.build(args.tektagon, "tektagon")
    bridge = Bridge(fpga, tek, relay=not args.no_relay,
                    responder_mode=args.mode)
    if args.raw_log:
        bridge.raw_file = open(args.raw_log, "a")
    bridge.raw_frames = args.frame == "raw"
    bridge.start()
    bridge.note(f"bridge up: fpga={args.fpga} tektagon={args.tektagon} "
                f"relay={not args.no_relay} responder={args.mode}")

    server = serve(bridge, args.host, args.port)
    print(f"GUI on http://{args.host}:{args.port}  (Ctrl+C to stop)")

    if args.auto:
        bridge.send("fpga", protocol.canned_request_stream())

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        bridge.stop()


if __name__ == "__main__":
    main()
