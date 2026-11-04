"""Conservative message-time annotations; never align clocks implicitly."""
from datetime import datetime, timezone


def normalize_time(msg_type: str, fields: dict) -> dict:
    """Keep raw clock values and units, without guessing from their magnitude.

    ``time_usec`` is ambiguous in common.xml (including HIGHRES_IMU and
    OPTICAL_FLOW_RAD). SYSTEM_TIME.time_unix_usec explicitly denotes UNIX time.
    Receive timestamps are deliberately kept outside this annotation.
    """
    sources = []
    for name, unit in (("time_boot_ms", "ms"), ("time_usec", "us"),
                       ("time_unix_usec", "us"), ("time_boot_us", "us")):
        if name not in fields:
            continue
        value = fields[name]
        domain = "boot" if name.startswith("time_boot_") else "unknown"
        if name == "time_unix_usec" and msg_type == "SYSTEM_TIME":
            domain = "unix"
        item = {"field": name, "value": value, "unit": unit,
                "domain": domain, "seconds": value / (1000 if unit == "ms" else 1000000),
                "utc": None}
        if domain == "unix" and value > 0:
            try:
                item["utc"] = datetime.fromtimestamp(value / 1000000, timezone.utc).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        sources.append(item)
    return {"sources": sources, "clock_alignment": None}
