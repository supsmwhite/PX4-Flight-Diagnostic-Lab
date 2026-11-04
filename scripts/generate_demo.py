"""Generate synthetic MAVLink bytes locally; never open a network socket."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pymavlink.dialects.v20 import common
from px4_flight_diag.storage.session import dumps


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--output", type=Path, required=True)
    args = cli.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoder = common.MAVLink(None, srcSystem=2, srcComponent=1)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with args.output.open("x", encoding="utf-8") as out:
        for index in range(5):
            encoder.seq = index
            payload = common.MAVLink_heartbeat_message(2, 12, 0, 0, 3, 3).pack(encoder)
            out.write(dumps({"schema_version": 1, "kind": "udp_packet", "packet_index": index,
                             "recv_utc": (start + timedelta(seconds=index)).isoformat(),
                             "recv_monotonic_ns": index * 1_000_000_000,
                             "peer": ["127.0.0.1", 15000], "size_bytes": len(payload),
                             "data_b64": base64.b64encode(payload).decode("ascii")}) + "\n")
    print(f"Synthetic only: {args.output}")


if __name__ == "__main__":
    main()
