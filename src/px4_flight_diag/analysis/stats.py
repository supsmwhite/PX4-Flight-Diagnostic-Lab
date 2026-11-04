"""Online receive-time statistics, with no retained packet/record history."""
import base64
from collections import Counter


class Stats:
    def __init__(self):
        self._packets = self._bytes = self._records = self._messages = 0
        self._first_packet = self._last_packet = None
        self._errors = Counter()
        self._types = Counter()
        self._streams = {}
        self._sequence = {}

    def add_packet(self, packet: dict):
        self._packets += 1
        self._bytes += len(base64.b64decode(packet["data_b64"], validate=True))
        now = packet["recv_monotonic_ns"]
        if self._first_packet is None:
            self._first_packet = now
        self._last_packet = now

    def add_record(self, record: dict):
        self._records += 1
        if record["kind"] == "parse_error":
            self._errors[record["error_type"]] += 1
            return
        if record["kind"] != "message":
            return
        self._messages += 1
        typ = record["msg_type"]
        self._types[typ] += 1
        peer = tuple(record["peer"])
        identity = (peer, record["sysid"], record["compid"])
        key = identity + (typ,)
        now = record["recv_monotonic_ns"]
        if key not in self._streams:
            self._streams[key] = {"count": 0, "first": now, "last": now,
                                  "interval_count": 0, "interval_sum": 0,
                                  "interval_min": None, "interval_max": None,
                                  "negative_intervals": 0}
        stream = self._streams[key]
        if stream["count"]:
            delta = now - stream["last"]
            if delta < 0:
                stream["negative_intervals"] += 1
            else:
                stream["interval_count"] += 1
                stream["interval_sum"] += delta
                stream["interval_min"] = delta if stream["interval_min"] is None else min(delta, stream["interval_min"])
                stream["interval_max"] = delta if stream["interval_max"] is None else max(delta, stream["interval_max"])
        stream["count"] += 1
        stream["last"] = now
        seq = record["seq"]
        if identity not in self._sequence:
            self._sequence[identity] = {"last_seq": seq, "observed": 1, "candidate_missing": 0,
                                        "gap_events": 0, "duplicates": 0, "out_of_order": 0,
                                        "wraps": 0}
            return
        state = self._sequence[identity]
        state["observed"] += 1
        distance = (seq - state["last_seq"]) % 256
        if distance == 0:
            state["duplicates"] += 1
        elif distance < 128:
            if seq < state["last_seq"]:
                state["wraps"] += 1
            if distance > 1:
                state["candidate_missing"] += distance - 1
                state["gap_events"] += 1
            state["last_seq"] = seq
        else:
            # Keep the forward high-water mark; late packets must not invent gaps.
            state["out_of_order"] += 1

    def summary(self) -> dict:
        span = None if self._first_packet is None else (self._last_packet - self._first_packet) / 1e9
        streams = []
        for (peer, sysid, compid, typ), s in sorted(self._streams.items()):
            duration = (s["last"] - s["first"]) / 1e9
            intervals = s["interval_count"]
            streams.append({"peer": list(peer), "sysid": sysid, "compid": compid,
                            "msg_type": typ, "count": s["count"],
                            "first_monotonic_ns": s["first"], "last_monotonic_ns": s["last"],
                            "span_s": duration,
                            "frequency_hz": (s["count"] - 1) / duration if duration > 0 else None,
                            "interval_count": intervals,
                            "interval_mean_s": s["interval_sum"] / intervals / 1e9 if intervals else None,
                            "interval_min_s": s["interval_min"] / 1e9 if intervals else None,
                            "interval_max_s": s["interval_max"] / 1e9 if intervals else None,
                            "negative_intervals": s["negative_intervals"],
                            "heartbeat_max_interval_s": s["interval_max"] / 1e9 if typ == "HEARTBEAT" and intervals else None})
        sequence = [dict(peer=list(peer), sysid=sysid, compid=compid, **state)
                    for (peer, sysid, compid), state in sorted(self._sequence.items())]
        return {"packets": self._packets, "raw_bytes": self._bytes,
                "records": self._records, "messages": self._messages,
                "parse_errors": sum(self._errors.values()), "crc_errors": self._errors["crc"],
                "error_counts": dict(self._errors), "message_counts": dict(self._types),
                "packet_span_s": span, "raw_bytes_per_second": self._bytes / span if span and span > 0 else None,
                "streams": streams, "sequence": sequence,
                "sequence_note": "Modulo-256 forward distances 1..127; 128..255 are out-of-order. Gaps are candidate missing messages, not a measured loss rate; resets, filtering and reordering can cause them."}
