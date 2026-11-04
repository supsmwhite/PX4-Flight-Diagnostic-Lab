import base64

import pytest
from pymavlink.dialects.v20 import common as mavlink

from px4_flight_diag.communication.parser import Parser
from px4_flight_diag.analysis.stats import Stats
from px4_flight_diag.timebase.normalize import normalize_time


def packet(data, index=0, seconds=0, peer=("127.0.0.1", 14550)):
    return {"packet_index": index, "recv_utc": "2026-01-01T00:00:00Z",
            "recv_monotonic_ns": int(seconds * 1e9), "peer": list(peer),
            "data_b64": base64.b64encode(data).decode()}


def heartbeat(seq=0, v1=False, sysid=1, compid=1):
    writer = mavlink.MAVLink(None, srcSystem=sysid, srcComponent=compid)
    writer.seq = seq
    msg = mavlink.MAVLink_heartbeat_message(2, 3, 0, 0, 4, 3)
    return msg.pack(writer, force_mavlink1=v1)


@pytest.mark.parametrize("v1", [False, True])
def test_multiframe_fragment_and_metadata(v1):
    parser = Parser()
    a, b = heartbeat(7, v1), heartbeat(8, v1)
    assert parser.feed(packet(a[:5])) == []
    assert parser.pending_bytes() == 5
    records = parser.feed(packet(a[5:] + b, index=1))
    assert [r["seq"] for r in records] == [7, 8]
    assert all(r["packet_index"] == 1 for r in records)
    assert records[0]["frame_hex"] == a.hex()
    assert records[0]["fields"]["autopilot"] == 3
    assert parser.pending_bytes() == 0


def test_peers_do_not_share_partial_frames():
    parser = Parser()
    a = heartbeat(11)
    assert parser.feed(packet(a[:6])) == []
    other = parser.feed(packet(heartbeat(12), peer=("127.0.0.2", 14550)))
    assert other[0]["seq"] == 12
    assert parser.pending_bytes() == 6
    assert parser.feed(packet(a[6:]))[0]["seq"] == 11
    assert parser.pending_bytes() == 0


@pytest.mark.parametrize("v1", [False, True])
def test_crc_recovery(v1):
    parser = Parser()
    damaged = bytearray(heartbeat(4, v1))
    damaged[-1] ^= 0x80
    records = parser.feed(packet(bytes(damaged) + heartbeat(5, v1)))
    assert any(r.get("error_type") == "crc" for r in records)
    assert [r["seq"] for r in records if r["kind"] == "message"] == [5]
    assert parser.pending_bytes() == 0


def test_bad_bytes_and_invalid_base64():
    parser = Parser()
    assert parser.feed(packet(b"garbage"))[0]["error_type"] == "bad_data"
    p = packet(b"")
    p["data_b64"] = "not base64!"
    assert parser.feed(p)[0]["error_type"] == "invalid_base64"


def test_time_domains_conservative():
    boot = normalize_time("ATTITUDE", {"time_boot_ms": 1234})["sources"][0]
    assert (boot["domain"], boot["unit"], boot["seconds"]) == ("boot", "ms", 1.234)
    for name in ("HIGHRES_IMU", "OPTICAL_FLOW_RAD", "UNKNOWN"):
        assert normalize_time(name, {"time_usec": 123})["sources"][0]["domain"] == "unknown"
    unix = normalize_time("SYSTEM_TIME", {"time_unix_usec": 1700000000000000})["sources"][0]
    assert unix["domain"] == "unix"
    assert unix["utc"].endswith("+00:00")
    assert normalize_time("HEARTBEAT", {}) == {"sources": [], "clock_alignment": None}


def test_stats_heartbeat_frequency_and_bytes():
    stats, parser = Stats(), Parser()
    for seq, seconds in enumerate((0, 1, 3)):
        p = packet(heartbeat(seq), index=seq, seconds=seconds)
        stats.add_packet(p)
        for r in parser.feed(p):
            stats.add_record(r)
    summary = stats.summary()
    assert summary["packets"] == summary["messages"] == 3
    assert summary["raw_bytes"] == sum(len(heartbeat(i)) for i in range(3))
    assert summary["raw_bytes_per_second"] == summary["raw_bytes"] / 3
    stream = summary["streams"][0]
    assert stream["frequency_hz"] == pytest.approx(2 / 3)
    assert stream["interval_mean_s"] == 1.5
    assert stream["heartbeat_max_interval_s"] == 2
    assert stream["interval_min_s"] == 1


def test_sequence_wrap_gap_duplicate_reorder_and_peer_isolation():
    stats, parser = Stats(), Parser()
    for seq in (254, 255, 0, 2, 2, 1, 3):
        for r in parser.feed(packet(heartbeat(seq))):
            stats.add_record(r)
    for r in parser.feed(packet(heartbeat(100), peer=("127.0.0.2", 14550))):
        stats.add_record(r)
    summary = stats.summary()
    sequence = summary["sequence"][0]
    assert sequence["wraps"] == 1
    assert sequence["candidate_missing"] == 1
    assert sequence["duplicates"] == 1
    assert sequence["out_of_order"] == 1
    assert sequence["last_seq"] == 3
    assert summary["sequence"][1]["candidate_missing"] == 0
    assert summary["streams"][0]["frequency_hz"] is None
    assert summary["raw_bytes_per_second"] is None


def test_unknown_message_is_not_crc_verified_message():
    parser = Parser()
    frame = bytearray(heartbeat())
    frame[7:10] = (0xFFFFFF).to_bytes(3, "little")
    records = parser.feed(packet(frame + heartbeat(1)))
    assert records[0]["kind"] == "parse_error"
    assert records[0]["error_type"] == "unknown_message"
    assert records[0]["frame_hex"] == frame.hex()
    assert records[1]["kind"] == "message"
    assert parser.pending_bytes() == 0


def test_stats_crc_and_empty_summary():
    stats, parser = Stats(), Parser()
    assert stats.summary()["streams"] == []
    damaged = bytearray(heartbeat())
    damaged[-1] ^= 0x80
    for record in parser.feed(packet(damaged)):
        stats.add_record(record)
    assert stats.summary()["crc_errors"] == 1
    assert stats.summary()["messages"] == 0
