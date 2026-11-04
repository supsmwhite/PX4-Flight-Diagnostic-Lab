import base64
import json
import socket
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pymavlink.dialects.v20 import common
from starlette.websockets import WebSocketDisconnect

from px4_flight_diag.cli import process
from px4_flight_diag.analysis.report import generate_report
from px4_flight_diag.live.core import LiveBuffer
from px4_flight_diag.live.server import LiveRunner, create_app, run_live
from px4_flight_diag.storage.session import Session, dumps


def packet(index):
    mav=common.MAVLink(None,srcSystem=2,srcComponent=1);mav.seq=index%256
    data=common.MAVLink_heartbeat_message(2,12,81,196608,3,3).pack(mav)
    return dict(schema_version=1,kind='udp_packet',packet_index=index,
                recv_monotonic_ns=index*10000000,recv_utc='2026-11-04T00:00:00+00:00',
                peer=['127.0.0.1',14551],size_bytes=len(data),data_b64=base64.b64encode(data).decode())


def historical(tmp_path,count=5):
    p=tmp_path/'history';(p/'raw').mkdir(parents=True)
    (p/'raw/udp.jsonl').write_text(''.join(dumps(packet(i))+'\n' for i in range(count)))
    return p


def test_replay_no_udp_no_mutation(tmp_path,monkeypatch):
    path=historical(tmp_path);before=(path/'raw/udp.jsonl').read_bytes()
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('historical producer opened socket'))
    runner=LiveRunner(replay_session=path,speed=100,start_delay=0)
    runner.start();runner.thread.join(5)
    assert not runner.thread.is_alive() and runner.failure is None
    snap=runner.buffer.snapshot()
    assert snap['state']=='COMPLETED' and snap['messages']==5
    assert (path/'raw/udp.jsonl').read_bytes()==before
    assert not (path/'reports').exists()


def test_archive_not_dependent_on_clients_or_display_capacity(tmp_path):
    session=Session(tmp_path,'synthetic',{'mode':'synthetic'})
    buffer=LiveBuffer(max_points=2)
    def attitude_packets():
        mav=common.MAVLink(None,srcSystem=2,srcComponent=1)
        for i in range(100):
            p=packet(i);mav.seq=i
            data=common.MAVLink_attitude_message(i*10,.1,.2,0,0,0,0).pack(mav)
            p.update(data_b64=base64.b64encode(data).decode(),size_bytes=len(data))
            yield p
    result=process(attitude_packets(),session,observer=buffer)
    assert buffer.snapshot()['display_dropped']==98
    assert buffer.snapshot()['client_missed']==98
    assert result['messages']==100
    assert len((session.path/'raw/udp.jsonl').read_text().splitlines())==100
    assert len((session.path/'processed/messages.jsonl').read_text().splitlines())==100
    assert buffer.snapshot()['messages']==100
    assert generate_report(session.path).is_file()


def test_ws_disconnect_does_not_stop_producer_and_origin_guard(tmp_path):
    runner=LiveRunner(replay_session=historical(tmp_path,30),speed=10,start_delay=0)
    with TestClient(create_app(runner)) as client:
        assert client.get('/').status_code==200
        assert client.get('/app.js').status_code==200
        assert client.get('/plotly.min.js').status_code==200
        assert client.get('/anything').status_code==404
        assert client.get('/',headers={'host':'evil.example'}).status_code==400
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws',headers={'origin':'http://evil.example'}): pass
        with client.websocket_connect('/ws') as ws:
            assert ws.receive_json()['version']==1
        runner.thread.join(3)
        assert runner.buffer.snapshot()['state']=='COMPLETED'
        assert runner.buffer.snapshot()['messages']==30
    assert not runner.thread.is_alive()


def test_live_runner_loopback_records_and_analyzes(tmp_path):
    import time
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as reserve:
        reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    cfg=dict(bind_host='127.0.0.1',bind_port=port,duration_seconds=.5,
             drone='loopback-live',output_root=str(tmp_path))
    r=LiveRunner(config=cfg,start_delay=0);r.start()
    # Synthetic sender is test-only and sends solely to local loopback.
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sender:
        for i in range(20):
            sender.sendto(base64.b64decode(packet(i)['data_b64']),('127.0.0.1',port))
            time.sleep(.01)
    r.thread.join(3)
    assert r.failure is None and r.buffer.snapshot()['state']=='COMPLETED'
    s=Path(r.buffer.session)
    summary=json.loads((s/'reports/summary.json').read_text())
    assert summary['messages']>0 and summary['status']=='completed'
    assert len((s/'raw/udp.jsonl').read_text().splitlines())==summary['messages']
    assert generate_report(s).is_file()


def test_observer_failure_preserves_archive(tmp_path):
    class Broken:
        def push_packet(self, p): raise RuntimeError('display failed')
        def push_record(self, r): raise RuntimeError('display failed')
    s=Session(tmp_path,'observer-failure',{})
    result=process((packet(i) for i in range(4)),s,observer=Broken())
    assert result['messages']==4 and result['live_observer_errors']==8
    assert len((s.path/'processed/messages.jsonl').read_text().splitlines())==4


def test_failure_and_stop_interruptible(tmp_path):
    p=historical(tmp_path)
    (p/'raw/udp.jsonl').write_text('broken\n')
    r=LiveRunner(replay_session=p,start_delay=0);r.start();r.thread.join(3)
    assert r.buffer.snapshot()['state']=='ERROR'
    r=LiveRunner(replay_session=p,start_delay=100);r.start();r.close()
    assert not r.thread.is_alive()


@pytest.mark.parametrize('speed',[0,-1,float('nan'),float('inf')])
def test_invalid_speed(tmp_path,speed):
    with pytest.raises(ValueError):LiveRunner(replay_session=historical(tmp_path),speed=speed)


def test_validation_before_web_start(tmp_path):
    with pytest.raises(ValueError):run_live(replay_session=tmp_path,port=0)
    with pytest.raises(ValueError):run_live(config={'bind_port':8765})
    with pytest.raises(ValueError):LiveRunner(replay_session=tmp_path)
