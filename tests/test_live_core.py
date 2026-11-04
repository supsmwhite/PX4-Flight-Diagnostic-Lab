"""Synthetic core tests: no device, socket or archival I/O."""
import json
import math
from concurrent.futures import ThreadPoolExecutor

import pytest

from px4_flight_diag.analysis.telemetry import field_value, _value, series
from px4_flight_diag.live import LiveBuffer


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def packet(ns=100_000_000_000):
    return {'recv_monotonic_ns': ns, 'data_b64': '', 'peer': ['127.0.0.1', 14550]}


def record(typ='ATTITUDE', fields=None, ns=100_000_000_000, sysid=1, seq=0):
    return {'kind': 'message', 'peer': ['127.0.0.1', 14550], 'sysid': sysid,
            'compid': 1, 'seq': seq, 'msg_type': typ, 'recv_monotonic_ns': ns,
            'fields': {'roll': .5, 'pitch': -.1} if fields is None else fields}


def test_independent_state_clocks_and_finish():
    clock = Clock()
    live = LiveBuffer(clock=clock)
    assert live.snapshot()['state'] == 'WAITING'
    live.push_packet(packet())
    assert live.snapshot()['state'] == 'STALE'  # empty UDP is not valid data
    live.push_record(record())
    assert live.snapshot()['state'] == 'RECEIVING'
    clock.now += 2.01
    live.push_packet(packet())
    assert live.snapshot()['state'] == 'STALE'
    live.push_record(record('HEARTBEAT', {}))
    assert live.snapshot()['state'] == 'RECEIVING'
    clock.now += 5.01
    assert live.snapshot()['state'] == 'DISCONNECTED'
    live.finish()
    assert live.snapshot()['state'] == 'COMPLETED'
    live.finish('disk full')
    assert live.snapshot()['state'] == 'ERROR'
    assert live.snapshot()['error'] == 'disk full'
    with pytest.raises(RuntimeError):
        live.push_packet(packet())


def test_configurable_thresholds_and_independent_link_state():
    clock = Clock()
    live = LiveBuffer(clock=clock, stale_seconds=1, disconnect_seconds=3)
    assert live.snapshot()['link_state'] == 'WAITING'
    live.push_packet(packet())
    assert live.snapshot()['link_state'] == 'RECEIVING'
    assert live.snapshot()['state'] == 'STALE'
    live.push_record(record())
    clock.now += 1
    assert live.snapshot()['state'] == 'RECEIVING'
    clock.now += .01
    assert live.snapshot()['state'] == 'STALE'
    assert live.snapshot()['link_state'] == 'RECEIVING'
    clock.now += 2
    assert live.snapshot()['link_state'] == 'DISCONNECTED'
    live.finish()
    assert live.snapshot()['state'] == 'COMPLETED'
    assert live.snapshot()['link_state'] == 'DISCONNECTED'


def test_relative_receive_time_and_shared_units():
    clock = Clock()
    live = LiveBuffer(clock=clock)
    live.push_packet(packet())
    rec = record(fields={'roll': math.pi / 2, 'pitch': float('nan'), 'time_usec': 999999999999999},
                 ns=102_000_000_000)
    live.push_record(rec)
    point = live.snapshot()['points'][0]
    assert point['t'] == 2
    assert point['values'] == {'roll': 90, 'pitch': None}
    assert rec['fields']['roll'] == math.pi / 2
    assert _value is field_value
    assert series([rec], 'ATTITUDE', 'roll', scale=180/math.pi)['y'] == [90]
    live.push_record(record('DISTANCE_SENSOR', {'id': 2, 'orientation': 25,
        'min_distance': 10, 'max_distance': 500, 'current_distance': 123}))
    live.push_record(record('OPTICAL_FLOW_RAD', {'sensor_id': 3, 'quality': 0,
        'integration_time_us': 10000, 'integrated_x': 1.0, 'integrated_y': float('inf')}))
    points = live.snapshot()['points']
    assert points[-2]['sensor'] == [2, 25]
    assert points[-2]['values']['current_distance'] == 1.23
    assert points[-1]['sensor'] == 3
    assert points[-1]['values']['quality'] == 0
    assert points[-1]['values']['integrated_x'] is None
    json.dumps(live.snapshot(), allow_nan=False)


def test_prior_heartbeat_context_and_identity_isolation():
    clock = Clock()
    live = LiveBuffer(clock=clock)
    live.push_packet(packet())
    target = {'q': [1, 0, 0, 0], 'type_mask': 0}
    live.push_record(record('ATTITUDE_TARGET', target))
    live.push_record(record('HEARTBEAT', {'base_mode': 0}))
    live.push_record(record('ATTITUDE_TARGET', target))
    live.push_record(record('ATTITUDE_TARGET', target, sysid=2))
    live.push_record(record('HEARTBEAT', {'base_mode': 128, 'autopilot': 12, 'custom_mode': 6 << 16}))
    live.push_record(record('ATTITUDE_TARGET', target))
    clock.now += 2.001
    live.push_record(record('ATTITUDE_TARGET', target, ns=102_001_000_000))
    pts = live.snapshot()['points']
    assert [p['target_context'] for p in pts] == ['UNKNOWN_OR_STALE', 'DISARMED',
        'UNKNOWN_OR_STALE', 'ARMED_PX4_MODE_UNCONFIRMED_EXECUTION', 'UNKNOWN_OR_STALE']
    assert pts[0]['values'] == {'roll': 0, 'pitch': 0}
    assert len(live.snapshot()['sources']) == 2
    assert live.snapshot()['sources'][1]['heartbeat_state'] == 'WAITING'


def test_capacity_batch_expiry_and_client_cursor():
    clock = Clock()
    live = LiveBuffer(clock=clock, max_points=5, batch_size=2)
    live.push_packet(packet())
    for i in range(10):
        live.push_record(record(seq=i))
    snap = live.snapshot()
    assert [p['id'] for p in snap['points']] == [9, 10]
    assert snap['display_dropped'] == 5
    assert snap['client_missed'] == 8
    assert live.snapshot(after=10)['points'] == []
    assert live.snapshot(after=8)['client_missed'] == 0
    clock.now += 61
    snap = live.snapshot()
    assert snap['points'] == []
    assert snap['display_dropped'] == 5  # expiration is not capacity loss
    assert snap['client_missed'] == 10
    assert snap['cursor'] == 10


def test_snapshot_batch_hard_limit_and_detachment():
    live = LiveBuffer()
    live.push_packet(packet())
    for _ in range(2100):
        live.push_record(record())
    snap = live.snapshot()
    assert len(snap['points']) == 2000
    assert snap['client_missed'] == 100
    snap['points'][-1]['values']['roll'] = 999
    snap['sources'][0]['identity'][0] = 'changed'
    assert live.snapshot()['points'][-1]['values']['roll'] != 999
    assert live.snapshot()['sources'][0]['identity'][0] == '127.0.0.1'


def test_source_stream_stats_limits_preserve_totals():
    live = LiveBuffer(max_identities=2, max_streams=3)
    live.push_packet(packet())
    live.push_record(record(seq=0))
    live.push_record(record(seq=4))
    live.push_record(record(sysid=2))
    live.push_record(record('HEARTBEAT', {}, sysid=2))
    for i in range(3, 100):
        live.push_record(record(sysid=i))
    for i in range(100):
        live.push_record(record(f'UNIQUE_{i}', {}))
    snap = live.snapshot()
    assert snap['messages'] == 201
    assert len(snap['sources']) == 2
    assert sum(len(s['streams']) for s in snap['sources']) == 3
    assert snap['sequence_candidate_missing'] == 3
    assert snap['warnings']
    assert snap['stats_limited_records'] == 197
    stats = live.stats_summary()
    assert stats['messages'] == 201
    assert len(stats['message_counts']) <= 4
    assert len(stats['sequence']) == 2
    assert len(stats['streams']) == 3


def test_errors_invalid_sensor_and_no_raw_retention():
    live = LiveBuffer()
    live.push_packet(packet())
    live.push_record({'kind': 'parse_error', 'error_type': 'crc'})
    live.push_record(record('DISTANCE_SENSOR', {'id': []}))
    live.push_record(record('ATTITUDE_TARGET', {'q': [0, 0, 0, 0], 'type_mask': 128}))
    snap = live.snapshot()
    assert snap['crc_errors'] == snap['parse_errors'] == 1
    assert snap['conversion_errors'] == 1
    assert snap['points'][0]['values'] == {'roll': None, 'pitch': None}
    assert 'fields' not in repr(live._points)


def test_stream_age_frequency_and_heartbeat_independent():
    clock = Clock()
    live = LiveBuffer(clock=clock)
    live.push_packet(packet())
    live.push_record(record('HEARTBEAT', {}))
    live.push_record(record())
    clock.now += 3
    live.push_packet(packet(103_000_000_000))
    live.push_record(record(ns=103_000_000_000))
    snap = live.snapshot()
    assert snap['state'] == 'RECEIVING'
    source = snap['sources'][0]
    assert source['heartbeat_state'] == 'STALE'
    attitude = next(s for s in source['streams'] if s['msg_type'] == 'ATTITUDE')
    assert attitude['age_s'] == 0
    assert attitude['frequency_hz'] == pytest.approx(1/3)
    assert attitude['max_gap_s'] == 3


def test_fast_replay_also_expires_receive_time_window():
    clock = Clock()
    live = LiveBuffer(clock=clock)
    live.push_packet(packet())
    live.push_record(record())
    live.push_packet(packet(161_000_000_000))
    live.push_record(record(ns=161_000_000_000))
    snap = live.snapshot()
    assert [p['t'] for p in snap['points']] == [61]
    assert snap['display_dropped'] == 0
    assert snap['client_missed'] == 1


def test_final_window_persists_and_context_ignores_replay_speed():
    clock = Clock()
    live = LiveBuffer(clock=clock)
    live.push_packet(packet())
    live.push_record(record('HEARTBEAT', {'base_mode': 0}))
    clock.now += 10  # slow replay: evidence timestamps remain only one second apart
    live.push_record(record('ATTITUDE_TARGET', {'q': [1, 0, 0, 0], 'type_mask': 0},
                            ns=101_000_000_000))
    assert live.snapshot()['points'][0]['target_context'] == 'DISARMED'
    live.finish()
    clock.now += 100
    assert len(live.snapshot()['points']) == 1
    assert live.snapshot()['state'] == 'COMPLETED'


def test_readers_and_producer_are_thread_safe():
    live = LiveBuffer(max_points=50)
    live.push_packet(packet())
    def produce():
        for i in range(500):
            live.push_record(record(seq=i % 256))
    def consume():
        for _ in range(50):
            snap = live.snapshot()
            ids = [p['id'] for p in snap['points']]
            assert ids == sorted(set(ids))
            assert len(ids) <= 50
            json.dumps(snap, allow_nan=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(produce), *(pool.submit(consume) for _ in range(3))]
        for future in futures:
            future.result()
    assert live.snapshot()['messages'] == 500


@pytest.mark.parametrize('kwargs', [{'max_points': 0}, {'max_identities': True},
    {'max_streams': -1}, {'window_seconds': float('inf')}, {'batch_size': 2001},
    {'stale_seconds': 0}, {'stale_seconds': True}, {'disconnect_seconds': 1},
    {'disconnect_seconds': float('inf')}, {'stale_seconds': float('nan')}])
def test_invalid_limits(kwargs):
    with pytest.raises(ValueError):
        LiveBuffer(**kwargs)


@pytest.mark.parametrize('after', [-1, True, 1.5, '1'])
def test_invalid_cursor(after):
    with pytest.raises(ValueError):
        LiveBuffer().snapshot(after)
