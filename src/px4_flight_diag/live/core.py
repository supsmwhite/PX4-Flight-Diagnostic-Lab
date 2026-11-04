"""Bounded, passive live projection of already archived telemetry.

One producer calls push_packet/push_record in receive order; any number of readers
may take snapshots. No sockets, worker threads, disk writes or retained raw fields.
Display resource limits never authorize dropping the caller's archival records.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import math
import threading
import time
from typing import Callable

from ..analysis.stats import Stats
from ..analysis import telemetry


class _BoundedStats(Stats):
    """Keep Stats semantics for admitted streams and totals for all records."""

    def __init__(self, identities: int, streams: int):
        super().__init__()
        self.identity_limit = identities
        self.stream_limit = streams
        self.limited = 0

    def add_record(self, record: dict) -> bool:
        if record['kind'] == 'parse_error':
            # Parser categories are finite; defensive callers cannot grow Counter.
            category = record['error_type']
            if category not in ('crc', 'bad_data', 'unknown_message', 'invalid_base64'):
                record = dict(record, error_type='other')
            super().add_record(record)
            return False
        if record['kind'] != 'message':
            super().add_record(record)
            return False
        identity = (tuple(record['peer']), record['sysid'], record['compid'])
        key = identity + (record['msg_type'],)
        if ((identity not in self._sequence and len(self._sequence) >= self.identity_limit)
                or (key not in self._streams and len(self._streams) >= self.stream_limit)):
            self._records += 1
            self._messages += 1
            typ = record['msg_type']
            self._types[typ if typ in self._types else '__limited__'] += 1
            self.limited += 1
            return False
        super().add_record(record)
        return True


_FIELDS = {
    'ATTITUDE': ('roll', 'pitch'),
    'ATTITUDE_TARGET': ('roll_deg', 'pitch_deg'),
    'LOCAL_POSITION_NED': ('x', 'y', 'z', 'vx', 'vy', 'vz'),
    'DISTANCE_SENSOR': ('current_distance', 'min_distance', 'max_distance'),
    'OPTICAL_FLOW_RAD': ('integrated_x', 'integrated_y', 'integrated_xgyro',
                         'integrated_ygyro', 'integrated_zgyro', 'quality',
                         'integration_time_us', 'distance'),
}


class LiveBuffer:
    """Thread-safe bounded display buffer, independent of polling clients.

    ``clock`` is execution monotonic seconds (injectable in tests). Point ``t``
    instead uses packet receive monotonic timestamps relative to the first packet;
    neither time_usec nor boot time is aligned. Stream frequencies/max gaps use
    receive timestamps, ages use execution clock. Limits are global, not per peer.
    ``display_dropped`` counts capacity/resource rejection, not normal expiry;
    ``client_missed`` counts unavailable or batch-skipped point IDs for this poll.
    """

    def __init__(self, session: str = '', mode: str = 'live', *,
                 max_points: int = 20000, window_seconds: float = 60.0,
                 max_identities: int = 64, max_streams: int = 512,
                 batch_size: int = 2000, stale_seconds: float = 2.0,
                 disconnect_seconds: float = 5.0,
                 clock: Callable[[], float] = time.monotonic):
        for name, value in (('max_points', max_points), ('max_identities', max_identities),
                            ('max_streams', max_streams), ('batch_size', batch_size)):
            if type(value) is not int or value <= 0:
                raise ValueError(f'{name} must be a positive integer')
        if batch_size > 2000:
            raise ValueError('batch_size must not exceed 2000')
        if telemetry.finite(window_seconds) is None or window_seconds <= 0:
            raise ValueError('window_seconds must be positive and finite')
        if (telemetry.finite(stale_seconds) is None or stale_seconds <= 0
                or telemetry.finite(disconnect_seconds) is None
                or disconnect_seconds < stale_seconds):
            raise ValueError('timeouts must be finite and 0 < stale_seconds <= disconnect_seconds')
        self._stale = float(stale_seconds)
        self._disconnect = float(disconnect_seconds)
        self.session, self.mode = str(session), str(mode)
        self._clock = clock
        self._lock = threading.Lock()
        self._points: deque = deque(maxlen=max_points)
        self._window = float(window_seconds)
        self._batch = batch_size
        self._stats = _BoundedStats(max_identities, max_streams)
        self._sources: dict = {}
        self._stream_clocks: dict = {}
        self._origin_ns: int | None = None
        self._latest_t = 0.0
        self._last_packet: float | None = None
        self._last_valid: float | None = None
        self._cursor = self._dropped = self._conversion_errors = 0
        self._finished = False
        self._error: str | None = None

    def _expire(self, now: float) -> None:
        # Preserve the final bounded window for post-run inspection.
        if self._finished:
            return
        while self._points and (now - self._points[0][0] > self._window
                                or self._latest_t - self._points[0][1]['t'] > self._window):
            self._points.popleft()

    def push_packet(self, packet: dict) -> None:
        """Observe a packet after raw archival, including empty datagrams."""
        with self._lock:
            if self._finished:
                raise RuntimeError('live buffer is finished')
            self._stats.add_packet(packet)
            now = self._clock()
            if self._origin_ns is None:
                self._origin_ns = packet['recv_monotonic_ns']
            self._last_packet = now
            self._latest_t = max(self._latest_t, (packet['recv_monotonic_ns'] - self._origin_ns) / 1e9)
            self._expire(now)

    def push_record(self, record: dict) -> None:
        """Observe an archived Parser record; invalid plot values become None."""
        with self._lock:
            if self._finished:
                raise RuntimeError('live buffer is finished')
            now = self._clock()
            self._expire(now)
            admitted = self._stats.add_record(record)
            if record['kind'] != 'message':
                return
            self._last_valid = now
            if not admitted:
                self._dropped += 1
                return
            identity = (*record['peer'], record['sysid'], record['compid'])
            typ = record['msg_type']
            source = self._sources.setdefault(identity, {'heartbeat': None})
            self._stream_clocks[identity + (typ,)] = now
            if typ == 'HEARTBEAT':
                fields = record['fields']
                # Only three bounded scalar context fields, never the raw dict.
                source['heartbeat'] = {
                    'at': now, 'ns': record['recv_monotonic_ns'],
                    **{k: fields.get(k) if type(fields.get(k)) is int else None
                       for k in ('base_mode', 'custom_mode', 'autopilot')},
                }
            if typ not in _FIELDS:
                return
            try:
                sensor = telemetry._sensor(record)
                values = {}
                for field in _FIELDS[typ]:
                    value = telemetry.field_value(record, field)
                    if typ == 'ATTITUDE' and value is not None:
                        value = telemetry.finite(math.degrees(value))
                    values[field.removesuffix('_deg')] = value
                if self._origin_ns is None:
                    raise ValueError('push_packet must precede plotted records')
                t = telemetry.finite((record['recv_monotonic_ns'] - self._origin_ns) / 1e9)
                if t is None:
                    raise ValueError('invalid receive timestamp')
            except (ValueError, TypeError, KeyError, OverflowError):
                self._conversion_errors += 1
                self._dropped += 1
                return
            self._latest_t = max(self._latest_t, t)
            self._expire(now)
            self._cursor += 1
            point = {'id': self._cursor, 't': t, 'identity': list(identity),
                     'msg_type': typ, 'sensor': list(sensor) if isinstance(sensor, tuple) else sensor,
                     'values': values}
            if typ == 'ATTITUDE_TARGET':
                point['target_context'] = self._target_context(source['heartbeat'], record)
            if len(self._points) == self._points.maxlen:
                self._dropped += 1
            self._points.append((now, point))

    @staticmethod
    def _target_context(heartbeat: dict | None, record: dict) -> str:
        # Context is evidence-time based, independent of replay speed.
        if (heartbeat is None
                or not 0 <= (record['recv_monotonic_ns'] - heartbeat['ns']) / 1e9 <= 2):
            return 'UNKNOWN_OR_STALE'
        mode = heartbeat['base_mode']
        if mode is None or not 0 <= mode <= 255:
            return 'UNKNOWN'
        if not mode & 128:
            return 'DISARMED'
        custom = heartbeat['custom_mode']
        if (heartbeat['autopilot'] == 12 and custom is not None
                and 0 <= custom <= 0xffffffff and (custom >> 16) & 255 in (2, 3, 4, 5, 6, 7, 8)):
            return 'ARMED_PX4_MODE_UNCONFIRMED_EXECUTION'
        return 'MODE_UNCONFIRMED'

    def finish(self, error: str | None = None) -> None:
        """Mark worker completion; failure takes precedence over all age states."""
        with self._lock:
            self._finished = True
            if error is not None:
                self._error = str(error)

    def stats_summary(self) -> dict:
        """Detached Stats summary; resource omissions are explicitly reported."""
        with self._lock:
            result = self._stats.summary()
            result['live_stats_limited_records'] = self._stats.limited
            return result

    @staticmethod
    def _age(now: float, timestamp: float | None) -> float | None:
        return None if timestamp is None else max(0.0, now - timestamp)

    def snapshot(self, after: int = 0) -> dict:
        """Version-1 JSON-safe snapshot; newest batch wins over slow clients."""
        if type(after) is not int or after < 0:
            raise ValueError('after must be a nonnegative integer')
        with self._lock:
            now = self._clock()
            self._expire(now)
            packet_age = self._age(now, self._last_packet)
            valid_age = self._age(now, self._last_valid)
            if self._finished:
                state = 'ERROR' if self._error is not None else 'COMPLETED'
            elif packet_age is None:
                state = 'WAITING'
            elif packet_age > self._disconnect:
                state = 'DISCONNECTED'
            elif valid_age is None or valid_age > self._stale:
                state = 'STALE'
            else:
                state = 'RECEIVING'
            # Reverse iteration touches at most batch_size candidates, not history.
            points = []
            for _, point in reversed(self._points):
                if point['id'] <= after or len(points) >= self._batch:
                    break
                points.append(deepcopy(point))
            points.reverse()
            sources = []
            for identity, source in self._sources.items():
                hb = source['heartbeat']
                age = self._age(now, hb['at'] if hb else None)
                sources.append({'identity': list(identity), 'heartbeat_age_s': age,
                                'heartbeat_state': 'WAITING' if age is None else
                                ('STALE' if age > self._stale else 'RECEIVING'), 'streams': []})
            source_map = {tuple(s['identity']): s for s in sources}
            for (peer, sysid, compid, typ), stream in self._stats._streams.items():
                identity = (*peer, sysid, compid)
                age = self._age(now, self._stream_clocks[identity + (typ,)])
                span = (stream['last'] - stream['first']) / 1e9
                source_map[identity]['streams'].append({
                    'msg_type': typ, 'count': stream['count'],
                    'frequency_hz': (stream['count'] - 1) / span if span > 0 else None,
                    'max_gap_s': None if stream['interval_max'] is None else stream['interval_max'] / 1e9,
                    'age_s': age, 'state': 'STALE' if age > self._stale else 'RECEIVING'})
            limited = self._stats.limited
            link_state = ('WAITING' if packet_age is None else
                          'DISCONNECTED' if packet_age > self._disconnect else 'RECEIVING')
            return {'version': 1, 'state': state, 'link_state': link_state,
                    'session': self.session, 'mode': self.mode,
                    'messages': self._stats._messages, 'crc_errors': self._stats._errors['crc'],
                    'parse_errors': sum(self._stats._errors.values()),
                    'conversion_errors': self._conversion_errors,
                    'sequence_candidate_missing': sum(s['candidate_missing'] for s in self._stats._sequence.values()),
                    'last_data_age_s': packet_age, 'last_message_age_s': valid_age,
                    'sources': sources, 'points': points, 'cursor': self._cursor,
                    'display_dropped': self._dropped,
                    'client_missed': max(0, self._cursor - after - len(points)),
                    'finished': self._finished, 'error': self._error,
                    'stats_limited_records': limited,
                    'warnings': ['Source/stream limit reached; detailed statistics and display are partial; archival is independent.'] if limited else []}
