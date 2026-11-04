"""Receive only: no MAVLink connection, heartbeat, or transmission."""
from __future__ import annotations

import base64
import socket
import time
from datetime import datetime, timezone
from typing import Callable, Iterator


def receive_packets(bind_host: str, bind_port: int, duration: float,
                    ready: Callable[[tuple], None] | None = None,
                    stop: Callable[[], bool] | None = None) -> Iterator[dict]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((bind_host, bind_port))
        if ready:
            ready(sock.getsockname())
        end = time.monotonic() + duration
        index = 0
        while (remaining := end - time.monotonic()) > 0 and not (stop and stop()):
            sock.settimeout(min(0.25, remaining))
            try:
                data, peer = sock.recvfrom(65535)
            except socket.timeout:
                continue
            mono = time.monotonic_ns()
            utc = datetime.now(timezone.utc).isoformat()
            yield {"schema_version": 1, "kind": "udp_packet", "packet_index": index,
                   "recv_utc": utc, "recv_monotonic_ns": mono,
                   "peer": list(peer), "size_bytes": len(data),
                   "data_b64": base64.b64encode(data).decode("ascii")}
            index += 1
