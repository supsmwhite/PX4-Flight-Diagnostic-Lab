"""Offline reports and source-preserving CLI integration."""
import hashlib
import json
import socket
from pathlib import Path

import pytest

from px4_flight_diag.analysis.report import build_figures, choose_clock, control_context, generate_report
from px4_flight_diag.analysis.telemetry import load_session
from px4_flight_diag.cli import main


def record(typ, fields, t=0, sysid=2):
    return dict(schema_version=1, kind='message', packet_index=t, recv_utc='2026-11-04T03:00:00+00:00',
                recv_monotonic_ns=1_000_000_000+t*1_000_000_000, peer=['127.0.0.1',14551],
                sysid=sysid, compid=1, seq=t%256, msg_type=typ, fields=fields, frame_hex='',
                time={'sources':[], 'clock_alignment':None})


def session(tmp_path, records):
    (tmp_path/'processed').mkdir()
    (tmp_path/'processed/messages.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in records))
    return tmp_path


def test_five_figures_units_axes_and_invalid_target(tmp_path):
    rr=[]
    for t in range(2):
        rr += [record('ATTITUDE',dict(time_boot_ms=t*1000,roll=0.5,pitch=0),t),
               record('ATTITUDE_TARGET',dict(time_boot_ms=t*1000,q=[1,0,0,0],type_mask=128),t),
               record('LOCAL_POSITION_NED',dict(time_boot_ms=t*1000,x=1,y=2,vx=3,vy=4),t),
               record('OPTICAL_FLOW_RAD',dict(time_usec=t*1000000,sensor_id=0,integrated_x=.01,integrated_y=.02,quality=80,integration_time_us=20000),t),
               record('DISTANCE_SENSOR',dict(time_boot_ms=t*1000,id=0,orientation=25,current_distance=4,min_distance=2,max_distance=600),t)]
    data=load_session(session(tmp_path,rr)); figs=build_figures(data,data.identities[0])
    assert len(figs)==5
    assert figs[0]['time_axis']=='boot' and figs[3]['time_axis']=='receive'
    assert figs[0]['status']=='INSUFFICIENT_DATA'
    assert figs[0]['figure'].data[0].y[0]==pytest.approx(28.64788976)
    assert figs[0]['figure'].data[1].y[0] is None
    assert figs[2]['figure'].data[0].y[0]==3
    assert figs[4]['figure'].data[0].y[0]==.04
    assert None in figs[1]['figure'].data[0].y  # 1s gap is not interpolated
    assert all(not trace.connectgaps for item in figs for trace in item['figure'].data)


def test_clock_reversal_and_unknown_never_aligned():
    rr=[record('ATTITUDE',{'time_boot_ms':1000}),record('ATTITUDE',{'time_boot_ms':500},1)]
    assert choose_clock(rr,('ATTITUDE',),'auto')[0]=='receive'
    assert choose_clock([record('OPTICAL_FLOW_RAD',{'time_usec':1000000})],('OPTICAL_FLOW_RAD',),'auto')[0]=='receive'
    assert choose_clock([record('ATTITUDE',{'time_boot_ms':1000})],('ATTITUDE',),'receive')[0]=='receive'


def test_control_context_expiry_and_disarmed():
    rr=[record('HEARTBEAT',dict(base_mode=81,autopilot=12,custom_mode=196608)),
        record('ATTITUDE_TARGET',{},1),record('ATTITUDE_TARGET',{},4)]
    ctx=control_context(rr)
    assert ctx['未解锁：仅显示目标消息，不代表执行控制']==1
    assert ctx['状态未知/心跳过期']==1


def test_html_standalone_no_overwrite_no_network_and_cli(tmp_path,monkeypatch):
    path=session(tmp_path,[record('ATTITUDE',dict(time_boot_ms=1000,roll=0,pitch=0))])
    (path/'metadata.json').write_text(json.dumps({'note':'</script><script>alert(1)</script>'}))
    before={p:hashlib.sha256(p.read_bytes()).digest() for p in path.rglob('*') if p.is_file()}
    monkeypatch.setattr(socket,'socket',lambda *a,**k:pytest.fail('analyze attempted network'))
    first=generate_report(path); second=generate_report(path)
    assert first.name=='diagnostic.html' and second.name=='diagnostic-001.html'
    text=first.read_text()
    assert 'plotly.js v' in text
    assert '<script src=' not in text
    assert text.count('class="plotly-graph-div"')==5
    assert '&lt;/script&gt;&lt;script&gt;alert(1)' in text
    assert 'INSUFFICIENT_DATA' in text
    assert main(['analyze','--session',str(path),'--time-axis','receive'])==0
    assert (path/'reports/diagnostic-002.html').is_file()
    assert all(hashlib.sha256(p.read_bytes()).digest()==digest for p,digest in before.items())


def test_multiple_sources_separate_and_empty_report(tmp_path):
    path=session(tmp_path,[record('ATTITUDE',dict(time_boot_ms=0,roll=0,pitch=0),sysid=2),
                           record('ATTITUDE',dict(time_boot_ms=0,roll=1,pitch=0),sysid=3)])
    text=generate_report(path).read_text()
    assert 'SYSID=2' in text and 'SYSID=3' in text
    assert text.count('class="plotly-graph-div"')==10


def test_unknown_and_known_flow_sensors_are_separate(tmp_path):
    rr=[]
    for t in range(2):
        for sensor in (None, 1):
            rr.append(record('OPTICAL_FLOW_RAD',dict(sensor_id=sensor, integrated_x=.1,
                integrated_y=.2, quality=80, integration_time_us=20000), t))
    data=load_session(session(tmp_path,rr))
    fig=build_figures(data,data.identities[0])[3]
    assert len(fig['figure'].data)==8
    assert all(q['valid']==2 for q in fig['quality'])


def test_empty_and_bad_input_cli(tmp_path):
    path=session(tmp_path,[])
    assert 'INSUFFICIENT_DATA' in generate_report(path).read_text()
    assert main(['analyze','--session',str(path),'--gap-seconds','nan'])==2
    (path/'processed/messages.jsonl').write_text('{broken\n')
    assert main(['analyze','--session',str(path)])==2
