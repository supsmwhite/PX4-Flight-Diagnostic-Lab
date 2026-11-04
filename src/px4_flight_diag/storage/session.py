"""Immutable-input, exclusive-output session storage."""
from __future__ import annotations

import base64
import binascii
import json
import math
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


def json_safe(value: Any) -> Any:
    """Strict JSON: preserve non-finite MAVLink floats as explicit strings."""
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return {"bytes_hex": value.hex()}
    return value


def dumps(value: Any) -> str:
    return json.dumps(json_safe(value), ensure_ascii=False, allow_nan=False)


def read_packets(path: Path) -> Iterator[dict]:
    """Fail visibly on damaged/truncated logs, retaining source and line number."""
    previous_ns: int | None = None
    previous_index = -1
    with path.open(encoding="utf-8") as source:
        for line_no, line in enumerate(source, 1):
            try:
                record = json.loads(line)
                if record.get("schema_version") != SCHEMA_VERSION or record.get("kind") != "udp_packet":
                    raise ValueError("unsupported raw record schema/kind")
                index = record["packet_index"]
                ns = record["recv_monotonic_ns"]
                peer = record["peer"]
                utc = datetime.fromisoformat(record["recv_utc"])
                if utc.utcoffset() is None:
                    raise ValueError("recv_utc must include timezone")
                if type(index) is not int or index <= previous_index:
                    raise ValueError("packet_index must strictly increase")
                if type(ns) is not int or ns < 0 or (previous_ns is not None and ns < previous_ns):
                    raise ValueError("monotonic receive time must not decrease")
                if not isinstance(peer, list) or len(peer) != 2 or not isinstance(peer[0], str) or type(peer[1]) is not int or not 0 <= peer[1] <= 65535:
                    raise ValueError("invalid peer")
                data = base64.b64decode(record["data_b64"], validate=True)
                if record.get("size_bytes") != len(data):
                    raise ValueError("size_bytes does not match payload")
                previous_ns, previous_index = ns, index
                yield record
            except (ValueError, KeyError, TypeError, AttributeError, binascii.Error) as exc:
                raise ValueError(f"{path}:{line_no}: invalid raw record: {exc}") from exc


class Session:
    def __init__(self, root: Path, drone: str, metadata: dict):
        if not drone or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in drone):
            raise ValueError("drone must contain only letters, digits, '_' or '-'")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        self.path = root / drone / f"{stamp}-{uuid.uuid4().hex[:8]}"
        self.path.mkdir(parents=True, exist_ok=False)
        for name in ("raw", "processed", "plots", "reports"):
            (self.path / name).mkdir()
        self.raw = (self.path / "raw/udp.jsonl").open("x", encoding="utf-8")
        self.messages = (self.path / "processed/messages.jsonl").open("x", encoding="utf-8")
        self.save_json("metadata.json", {"schema_version": 1, **metadata})

    def save_json(self, relative: str, value: dict) -> None:
        with (self.path / relative).open("x", encoding="utf-8") as handle:
            handle.write(dumps(value) + "\n")

    def packet(self, packet: dict) -> None:
        self.raw.write(dumps(packet) + "\n")
        self.raw.flush()  # Persist before parsing; fsync at clean close.

    def record(self, record: dict) -> None:
        self.messages.write(dumps({"schema_version": 1, **record}) + "\n")
        self.messages.flush()

    def close(self) -> None:
        for handle in (self.raw, self.messages):
            if not handle.closed:
                try:
                    handle.flush()
                    os.fsync(handle.fileno())
                finally:
                    handle.close()
