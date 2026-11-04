import base64
import json
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pymavlink.dialects.v20 import common

from px4_flight_diag.cli import load_config, main, process
from px4_flight_diag.collectors.udp import receive_packets
from px4_flight_diag.storage.session import Session, dumps, read_packets


def packet(index=0):
    encoder = common.MAVLink(None, srcSystem=2, srcComponent=1)
    encoder.seq = index
    data = common.MAVLink_heartbeat_message(2, 12, 0, 0, 3, 3).pack(encoder)
    return {"schema_version": 1, "kind": "udp_packet", "packet_index": index,
            "recv_utc": "2026-01-01T00:00:00+00:00", "recv_monotonic_ns": index * 10**9,
            "peer": ["127.0.0.1", 15000], "size_bytes": len(data),
            "data_b64": base64.b64encode(data).decode()}


def test_offline_cli_never_uses_socket(tmp_path, monkeypatch, capsys):
    source = tmp_path / "input.jsonl"
    source.write_text("".join(dumps(packet(i)) + "\n" for i in range(3)))
    before = source.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("replay must not open network")
    monkeypatch.setattr(socket, "socket", forbidden)
    assert main(["replay", "--input", str(source), "--output-root", str(tmp_path)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["messages"] == 3
    assert source.read_bytes() == before
    session = Path(summary["session"])
    assert (session / "raw/udp.jsonl").read_bytes() == before
    assert len((session / "processed/messages.jsonl").read_text().splitlines()) == 3
    assert summary["status"] == "completed"


def test_loopback_receive_and_replay(tmp_path):
    # Only tests transmit, only to the OS-selected loopback port.
    ready = threading.Event()
    address = []
    results = []
    errors = []
    session = Session(tmp_path, "drone2", {"mode": "test-loopback"})
    def bound(endpoint):
        address.append(endpoint)
        ready.set()
    def collect():
        try:
            results.append(process(receive_packets("127.0.0.1", 0, 0.5, bound), session))
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=collect)
    thread.start()
    try:
        assert ready.wait(3)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
            for i in range(3):
                sender.sendto(base64.b64decode(packet(i)["data_b64"]), address[0])
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
    assert results[0]["messages"] == 3
    saved = list(read_packets(session.path / "raw/udp.jsonl"))
    assert len(saved) == 3
    replay = Session(tmp_path, "replay", {"mode": "test-replay"})
    assert process(iter(saved), replay)["messages"] == 3


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"data_b64": "!"}, {"size_bytes": 0},
    {"peer": []}, {"recv_utc": "2026-01-01"}, {"recv_monotonic_ns": -1},
])
def test_invalid_raw_records(tmp_path, change):
    record = {**packet(), **change}
    path = tmp_path / "bad.jsonl"
    path.write_text(dumps(record) + "\n")
    with pytest.raises(ValueError, match=":1: invalid raw record"):
        list(read_packets(path))


def test_decreasing_clock_and_truncation(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(dumps(packet(2)) + "\n" + dumps(packet(1)) + "\n")
    with pytest.raises(ValueError):
        list(read_packets(path))
    path.write_text('{"schema_version":')
    with pytest.raises(ValueError):
        list(read_packets(path))


def test_failed_replay_has_report(tmp_path, capsys):
    path = tmp_path / "bad.jsonl"
    path.write_text("broken\n")
    assert main(["replay", "--input", str(path), "--output-root", str(tmp_path)]) == 2
    reports = list(tmp_path.glob("replay/*/reports/summary.json"))
    assert len(reports) == 1
    assert json.loads(reports[0].read_text())["status"] == "failed"


def test_session_unique_and_drone_safe(tmp_path):
    one, two = Session(tmp_path, "drone1", {}), Session(tmp_path, "drone1", {})
    one.close()
    two.close()
    assert one.path != two.path
    with pytest.raises(ValueError):
        Session(tmp_path, "../escape", {})
    with pytest.raises(FileExistsError):
        one.save_json("metadata.json", {})


def test_config_validation(tmp_path):
    config = load_config(Path("config/connection.example.yaml"))
    assert config["bind_port"] == 14551
    path = tmp_path / "invalid.yaml"
    path.write_text("bind_port: -1\n")
    with pytest.raises(ValueError):
        load_config(path)


def test_module_entry():
    result = subprocess.run([sys.executable, "-m", "px4_flight_diag", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "0.1.0"


def test_capture_cli_loopback(tmp_path):
    # Reserve an ephemeral local port; release before CLI binds it.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = tmp_path / "capture.yaml"
    config.write_text(f"bind_host: 127.0.0.1\nbind_port: {port}\nduration_seconds: 0.5\ndrone: drone2\noutput_root: {tmp_path}\n")
    result = subprocess.run([sys.executable, "-m", "px4_flight_diag", "capture", "--config", str(config)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["packets"] == 0
    assert summary["raw_bytes_per_second"] is None
    assert summary["status"] == "completed"
    assert "receive-only" in result.stderr


def test_strict_json_nonfinite():
    assert json.loads(dumps({"value": float("nan")})) == {"value": "nan"}
