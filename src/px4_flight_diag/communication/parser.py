"""Read-only incremental MAVLink common v2/v1 parsing, isolated per UDP peer."""
import base64
import binascii

from pymavlink.dialects.v20 import common as mavlink

from ..timebase.normalize import normalize_time


def _json_value(value):
    if isinstance(value, (bytes, bytearray)):
        return list(value)
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    if isinstance(value, dict):
        return {k: _json_value(v) for k, v in value.items()}
    return value


class Parser:
    """Frames are attributed to the packet that completes (or rejects) them.

    Input timestamps are not interpreted. No sockets, sending, or clock setting.
    Signed frames are decoded but signatures are NOT authenticated.
    """

    def __init__(self):
        self._buffers = {}
        self._decoders = {}

    def pending_bytes(self) -> int:
        return sum(len(buf) for buf in self._buffers.values())

    @staticmethod
    def _base(packet):
        return {"packet_index": packet["packet_index"], "recv_utc": packet["recv_utc"],
                "recv_monotonic_ns": packet["recv_monotonic_ns"],
                "peer": list(packet["peer"])}

    def _error(self, packet, data, reason, error_type):
        return dict(self._base(packet), kind="parse_error", sysid=None, compid=None,
                    msg_type=None, seq=None, fields={}, frame_hex=bytes(data).hex(),
                    time={"sources": [], "clock_alignment": None},
                    reason=reason, error_type=error_type)

    def feed(self, packet: dict) -> list[dict]:
        peer = tuple(packet["peer"])
        try:
            data = base64.b64decode(packet["data_b64"], validate=True)
        except (ValueError, binascii.Error, TypeError) as exc:
            return [self._error(packet, b"", str(exc), "invalid_base64")]
        buf = self._buffers.setdefault(peer, bytearray())
        decoder = self._decoders.setdefault(peer, mavlink.MAVLink(None))
        buf.extend(data)
        records = []
        while buf:
            if buf[0] not in (0xFD, 0xFE):
                stop = next((i for i, b in enumerate(buf) if b in (0xFD, 0xFE)), len(buf))
                records.append(self._error(packet, buf[:stop], "Bytes outside a MAVLink frame", "bad_data"))
                del buf[:stop]
                continue
            if len(buf) < 3:
                break
            v2 = buf[0] == 0xFD
            if v2 and buf[2] & ~1:
                records.append(self._error(packet, buf[:1], "Unsupported MAVLink incompatibility flags", "bad_data"))
                del buf[:1]
                continue
            length = buf[1] + (12 if v2 else 8) + (13 if v2 and buf[2] & 1 else 0)
            if len(buf) < length:
                break
            frame = bytes(buf[:length])
            try:
                message = decoder.decode(bytearray(frame))
            except Exception as exc:
                reason = str(exc)
                category = "crc" if "crc" in reason.lower() or "checksum" in reason.lower() else "bad_data"
                records.append(self._error(packet, frame, reason, category))
                # Rescan rather than discarding a corrupt length's entire span:
                # it may contain the beginning of the following valid frame.
                del buf[:1]
                continue
            del buf[:length]
            if message.get_type().startswith("UNKNOWN"):
                records.append(self._error(packet, frame,
                                           "Unknown common-dialect message ID; CRC cannot be verified",
                                           "unknown_message"))
                continue
            fields = _json_value(message.to_dict())
            fields.pop("mavpackettype", None)
            msg_type = message.get_type()
            records.append(dict(self._base(packet), kind="message",
                                sysid=message.get_srcSystem(), compid=message.get_srcComponent(),
                                msg_type=msg_type, seq=message.get_seq(), fields=fields,
                                frame_hex=frame.hex(), time=normalize_time(msg_type, fields)))
        return records
