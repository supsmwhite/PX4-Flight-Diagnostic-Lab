"""Offline schema-1 telemetry loading and conservative plotting-ready series.

No raw data is opened, no clocks are aligned, and input dictionaries are never
modified. Values remain raw except explicitly documented derived series.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any, Iterable

Identity = tuple[str, int, int, int]
Sensor = int | tuple[int, int | None]


@dataclass
class SessionData:
    path: Path
    metadata: dict
    summary: dict
    records: list[dict]
    errors: list[dict]
    warnings: list[str]

    @property
    def identities(self) -> list[Identity]:
        return sorted({_identity(record) for record in self.records})


def finite(value: Any) -> float | None:
    """Accept finite real JSON numbers only (not booleans or numeric strings)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def quaternion_euler(q: Any) -> tuple[float, float] | None:
    """Convert near-unit MAVLink [w,x,y,z] to roll/pitch degrees."""
    if not isinstance(q, (list, tuple)) or len(q) != 4:
        return None
    values = [finite(v) for v in q]
    if any(v is None for v in values):
        return None
    w, x, y, z = values
    norm = math.hypot(w, x, y, z)
    if not 0.9 <= norm <= 1.1:
        return None
    w, x, y, z = (v / norm for v in values)
    roll = math.atan2(2 * (w * x + y * z), 1 - 2 * (x * x + y * y))
    pitch = math.asin(max(-1.0, min(1.0, 2 * (w * y - z * x))))
    return math.degrees(roll), math.degrees(pitch)


def _integer(value: Any, low: int, high: int | None = None) -> bool:
    return type(value) is int and value >= low and (high is None or value <= high)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _validate(record: Any) -> None:
    if not isinstance(record, dict):
        raise ValueError("record must be an object")
    if type(record.get("schema_version")) is not int or record["schema_version"] != 1:
        raise ValueError("unsupported processed schema_version")
    if record.get("kind") not in ("message", "parse_error"):
        raise ValueError("unsupported processed kind")
    if not _integer(record.get("packet_index"), 0) or not _integer(record.get("recv_monotonic_ns"), 0):
        raise ValueError("invalid packet_index/recv_monotonic_ns")
    utc = datetime.fromisoformat(record["recv_utc"])
    if utc.utcoffset() is None:
        raise ValueError("recv_utc must include timezone")
    peer = record.get("peer")
    if not isinstance(peer, list) or len(peer) != 2 or not isinstance(peer[0], str) or not _integer(peer[1], 0, 65535):
        raise ValueError("invalid peer")
    if not isinstance(record.get("fields"), dict):
        raise ValueError("fields must be an object")
    frame = record.get("frame_hex")
    if not isinstance(frame, str) or len(frame) % 2 or any(c not in "0123456789abcdefABCDEF" for c in frame):
        raise ValueError("invalid frame_hex")
    time = record.get("time")
    if not isinstance(time, dict) or not isinstance(time.get("sources"), list) or "clock_alignment" not in time:
        raise ValueError("invalid time annotation")
    for source in time["sources"]:
        if not isinstance(source, dict) or not {"field", "value", "unit", "domain", "seconds", "utc"} <= source.keys():
            raise ValueError("invalid time source")
    if record["kind"] == "message":
        if not all(_integer(record.get(k), 0, 255) for k in ("sysid", "compid", "seq")):
            raise ValueError("invalid message identity/sequence")
        if not isinstance(record.get("msg_type"), str) or not record["msg_type"]:
            raise ValueError("invalid msg_type")
        _sensor(record)  # Reject malformed sensor keys before hashing/grouping.
    else:
        if any(record.get(k, False) is not None for k in ("sysid", "compid", "seq", "msg_type")) or record["fields"]:
            raise ValueError("invalid parse_error identity/fields")
        if not all(isinstance(record.get(k), str) for k in ("reason", "error_type")):
            raise ValueError("invalid parse_error reason/type")


def load_session(path: Path) -> SessionData:
    """Read processed/messages.jsonl and optional metadata/summary, never raw.

    Malformed processed JSON or structure raises ValueError with path and 1-based
    line. Missing optional documents produce warnings; malformed ones still fail.
    """
    path = Path(path)
    if not path.is_dir():
        raise ValueError(f"{path}: session must be a directory")
    warnings: list[str] = []

    def optional(relative: str) -> dict:
        source = path / relative
        if not source.exists():
            warnings.append(f"Missing optional {relative}")
            return {}
        try:
            value = json.loads(source.read_text(encoding="utf-8"), parse_constant=_reject_constant)
            if not isinstance(value, dict):
                raise ValueError("expected an object")
            return value
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"{source}: invalid JSON document: {exc}") from exc

    metadata = optional("metadata.json")
    summary = optional("reports/summary.json")
    records: list[dict] = []
    errors: list[dict] = []
    source = path / "processed/messages.jsonl"
    if not source.is_file():
        raise ValueError(f"{source}: missing processed messages")
    previous_ns = previous_index = -1
    with source.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                record = json.loads(line, parse_constant=_reject_constant)
                _validate(record)
                if record["recv_monotonic_ns"] < previous_ns:
                    raise ValueError("recv_monotonic_ns must not decrease")
                if record["packet_index"] < previous_index:
                    raise ValueError("packet_index must not decrease")
                previous_ns = record["recv_monotonic_ns"]
                previous_index = record["packet_index"]
            except (ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{source}:{line_no}: invalid processed record: {exc}") from exc
            (records if record["kind"] == "message" else errors).append(record)
    return SessionData(path, metadata, summary, records, errors, warnings)


def _identity(record: dict) -> Identity:
    return record["peer"][0], record["peer"][1], record["sysid"], record["compid"]


def _sensor(record: dict) -> Sensor | None:
    fields = record["fields"]
    keys = {"DISTANCE_SENSOR": ("id", "orientation"),
            "OPTICAL_FLOW_RAD": ("sensor_id",)}.get(record["msg_type"], ())
    for key in keys:
        if fields.get(key) is not None and not _integer(fields[key], 0, 255):
            raise ValueError(f"invalid {record['msg_type']} {key}: expected uint8 or missing")
    if record["msg_type"] == "DISTANCE_SENSOR":
        return fields.get("id"), fields.get("orientation")
    if record["msg_type"] == "OPTICAL_FLOW_RAD":
        return fields.get("sensor_id")
    return None


def _value(record: dict, field: str) -> float | None:
    fields = record["fields"]
    msg_type = record["msg_type"]
    if msg_type == "ATTITUDE_TARGET" and field in ("roll_deg", "pitch_deg"):
        mask = fields.get("type_mask")
        if not _integer(mask, 0, 255) or mask & 128:
            return None
        angles = quaternion_euler(fields.get("q"))
        return None if angles is None else angles[field == "pitch_deg"]
    if msg_type == "OPTICAL_FLOW_RAD":
        quality = fields.get("quality")
        valid_quality = _integer(quality, 0, 255)
        if field == "quality":
            return float(quality) if valid_quality else None
        integration = finite(fields.get("integration_time_us"))
        if integration is None or integration <= 0:
            return None
        if field.startswith("integrated_") and (not valid_quality or quality == 0):
            return None
    value = finite(fields.get(field))
    if msg_type == "DISTANCE_SENSOR" and field in ("min_distance", "max_distance", "current_distance"):
        low, high = finite(fields.get("min_distance")), finite(fields.get("max_distance"))
        if low is None or high is None or not 0 <= low <= high < 65535:
            return None
        if value is None or not 0 <= value < 65535:
            return None
        if field == "current_distance" and not low <= value <= high:
            return None
        return value / 100.0
    return value


def series(
    records: Iterable[dict], msg_type: str, field: str, *, scale: float = 1.0,
    time_axis: str = "receive", origin: float = 0.0, gap_seconds: float = 0.5,
    identity: Identity | None = None, sensor: Sensor | None = None,
) -> dict:
    """Return x/y lists, sample counts, gap_count and connectgaps=False.

    Identity and sensor selection precede sample counting. Ambiguous sources or
    sensor instances raise instead of mixing streams. Distance fields convert cm
    to m before scale; target roll_deg/pitch_deg derive from unmasked q. Other
    fields retain units (use scale=180/pi for ATTITUDE roll/pitch). Optical flow
    retains raw integrals but requires positive integration_time_us and uint8
    quality > 0. Quality itself is independent of the integration window and
    retains valid zero (unknown quality remains missing).

    Missing/nonfinite values are None, never zero. Missing time is (None,None).
    A >gap_seconds jump or clock reversal inserts (None,None) before the sample;
    boot time is exclusively fields.time_boot_ms, never time_usec. gap_count
    counts discontinuities between otherwise adjacent timed samples; invalid
    samples independently break lines without adding to gap_count.
    """
    if time_axis not in ("receive", "boot"):
        raise ValueError("time_axis must be 'receive' or 'boot'")
    scale_value, origin_value, gap = finite(scale), finite(origin), finite(gap_seconds)
    if scale_value is None or origin_value is None or gap is None or gap <= 0:
        raise ValueError("scale/origin must be finite and gap_seconds positive")
    selected = [r for r in records if r.get("kind") == "message" and r.get("msg_type") == msg_type]
    if identity is not None:
        selected = [r for r in selected if _identity(r) == identity]
    elif len({_identity(r) for r in selected}) > 1:
        raise ValueError(f"{msg_type}: multiple identities; select identity explicitly")
    if msg_type == "OPTICAL_FLOW_RAD" and isinstance(sensor, tuple):
        if len(sensor) != 2 or sensor[1] is not None:
            raise ValueError("optical flow sensor must be sensor_id or (sensor_id, None)")
        sensor = sensor[0]
    if sensor is not None:
        selected = [r for r in selected if _sensor(r) == sensor]
    elif len({_sensor(r) for r in selected}) > 1:
        raise ValueError(f"{msg_type}: multiple sensors; select sensor explicitly")
    x: list[float | None] = []
    y: list[float | None] = []
    valid_count = invalid_count = gap_count = 0
    previous: float | None = None
    for record in selected:
        raw_time = record.get("recv_monotonic_ns") if time_axis == "receive" else record["fields"].get("time_boot_ms")
        timestamp = finite(raw_time)
        if timestamp is not None and timestamp >= 0:
            timestamp = timestamp / (1e9 if time_axis == "receive" else 1000.0) - origin_value
            timestamp = finite(timestamp)
        else:
            timestamp = None
        value = _value(record, field)
        value = finite(value * scale_value) if value is not None else None
        if timestamp is not None and previous is not None and (timestamp < previous or timestamp - previous > gap):
            x.append(None)
            y.append(None)
            gap_count += 1
        if timestamp is None or value is None:
            invalid_count += 1
            value = None
        else:
            valid_count += 1
        x.append(timestamp)
        y.append(value)
        previous = timestamp
    return {"x": x, "y": y, "valid_count": valid_count,
            "invalid_count": invalid_count, "gap_count": gap_count, "connectgaps": False}
