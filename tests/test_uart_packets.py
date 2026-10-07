"""
Unit tests for uart_packets.py. Run anywhere (laptop, WSL, or the board):
    pytest -v test_uart_packets.py
"""
import pytest

from uart_packets import (
    ESCAPE, FRAME_FLAG, MSG_TYPE_SPDM, MctpPacket, FrameDecoder,
    build_mctp_packets, encode_frame, escape, fcs16,
)


def decode_all(wire: bytes, chunk: int = 1):
    dec = FrameDecoder()
    out = []
    for i in range(0, len(wire), chunk):
        out.extend(dec.feed(wire[i:i + chunk]))
    return out, dec


def pkt(payload=b"\x05\x10\x84\x00\x00", **kw):
    args = dict(dest_eid=0x10, src_eid=0x08, som=True, eom=True, seq=0,
                tag_owner=True, msg_tag=0, payload=payload)
    args.update(kw)
    return MctpPacket(**args)


# ---------- FCS-16 ----------
def test_fcs16_standard_check_value():
    # CRC-16/X-25 (RFC 1662 FCS-16) published check value for "123456789"
    assert fcs16(b"123456789") == 0x906E


def test_fcs16_detects_single_bit_flip():
    data = bytearray(b"caliptra-uart")
    good = fcs16(bytes(data))
    data[3] ^= 0x01
    assert fcs16(bytes(data)) != good


# ---------- Escaping ----------
@pytest.mark.parametrize("raw,expected", [
    (b"\x7E", b"\x7D\x5E"),
    (b"\x7D", b"\x7D\x5D"),
    (b"\x01\x7E\x02", b"\x01\x7D\x5E\x02"),
    (b"\x00\xFF", b"\x00\xFF"),
])
def test_escape(raw, expected):
    assert escape(raw) == expected


def test_encoded_frame_has_no_flags_inside():
    frame = encode_frame(pkt(payload=bytes([0x05]) + bytes([FRAME_FLAG, ESCAPE]) * 10))
    assert frame[0] == FRAME_FLAG and frame[-1] == FRAME_FLAG
    assert FRAME_FLAG not in frame[1:-1]


# ---------- MCTP header ----------
def test_header_bit_layout():
    raw = pkt(som=True, eom=False, seq=2, tag_owner=True, msg_tag=5).to_bytes()
    assert raw[:3] == bytes([0x01, 0x10, 0x08])
    # SOM=1 EOM=0 seq=10 TO=1 tag=101 -> 1010 1101
    assert raw[3] == 0b1010_1101


@pytest.mark.parametrize("seq", range(4))
@pytest.mark.parametrize("tag", [0, 3, 7])
def test_header_roundtrip(seq, tag):
    p = pkt(som=False, eom=True, seq=seq, msg_tag=tag, tag_owner=False)
    assert MctpPacket.from_bytes(p.to_bytes()) == p


def test_from_bytes_rejects_short():
    with pytest.raises(ValueError):
        MctpPacket.from_bytes(b"\x01\x10")


# ---------- Fragmentation ----------
def test_small_message_is_single_packet():
    pkts = build_mctp_packets(MSG_TYPE_SPDM, b"\x10\x84\x00\x00", 0x10, 0x08)
    assert len(pkts) == 1
    assert pkts[0].som and pkts[0].eom
    assert pkts[0].msg_type == MSG_TYPE_SPDM


def test_empty_message_still_carries_type():
    pkts = build_mctp_packets(MSG_TYPE_SPDM, b"", 0x10, 0x08)
    assert len(pkts) == 1 and pkts[0].payload == bytes([MSG_TYPE_SPDM])


@pytest.mark.parametrize("mtu", [8, 24, 64])
def test_fragmentation_flags_seq_and_size(mtu):
    msg = bytes(range(200))
    pkts = build_mctp_packets(MSG_TYPE_SPDM, msg, 0x10, 0x08, msg_tag=3, mtu=mtu)
    assert pkts[0].som and not any(p.som for p in pkts[1:])
    assert pkts[-1].eom and not any(p.eom for p in pkts[:-1])
    assert [p.seq for p in pkts] == [i % 4 for i in range(len(pkts))]  # wraps
    assert all(p.msg_tag == 3 for p in pkts)
    assert all(len(p.to_bytes()) <= mtu for p in pkts)
    assert b"".join(p.payload for p in pkts) == bytes([MSG_TYPE_SPDM]) + msg


def test_oversized_packet_rejected():
    with pytest.raises(ValueError):
        encode_frame(pkt(payload=bytes(300)))


# ---------- Decoder ----------
@pytest.mark.parametrize("chunk", [1, 3, 7, 1000])
def test_roundtrip_any_chunk_size(chunk):
    msg = bytes(range(0x60, 0x90)) * 3  # includes 0x7D/0x7E
    pkts = build_mctp_packets(MSG_TYPE_SPDM, msg, 0x10, 0x08, mtu=32)
    out, dec = decode_all(b"".join(encode_frame(p) for p in pkts), chunk)
    assert dec.errors == 0
    assert out == pkts


def test_leading_garbage_ignored():
    p = pkt()
    out, dec = decode_all(b"boot log noise\r\n\x00\x13" + encode_frame(p))
    assert out == [p]


def test_shared_flag_between_frames():
    a, b = pkt(msg_tag=1), pkt(msg_tag=2)
    fa, fb = encode_frame(a), encode_frame(b)
    out, dec = decode_all(fa + fb[1:])  # drop fb's opening flag
    assert out == [a, b] and dec.errors == 0


def test_bad_fcs_dropped_and_decoder_recovers():
    bad = bytearray(encode_frame(pkt(msg_tag=1)))
    bad[5] ^= 0xFF
    good = pkt(msg_tag=2)
    out, dec = decode_all(bytes(bad) + encode_frame(good))
    assert out == [good]
    assert dec.errors == 1


def test_bad_revision_rejected():
    frame = bytearray(encode_frame(pkt()))
    frame[1] = 0x02
    out, dec = decode_all(bytes(frame))
    assert out == [] and dec.errors == 1


def test_truncated_frame_rejected():
    frame = encode_frame(pkt())
    out, dec = decode_all(frame[:6] + bytes([FRAME_FLAG]))
    assert out == [] and dec.errors == 1


def test_empty_frames_are_not_errors():
    out, dec = decode_all(bytes([FRAME_FLAG] * 5))
    assert out == [] and dec.errors == 0
