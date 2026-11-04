"""Standalone offline HTML: derived artifacts only, no network or raw reparsing."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from html import escape
import json
import math
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .telemetry import SessionData, finite, load_session, series

KEY_TYPES = ('HEARTBEAT', 'ATTITUDE', 'ATTITUDE_TARGET', 'LOCAL_POSITION_NED',
             'POSITION_TARGET_LOCAL_NED', 'OPTICAL_FLOW_RAD', 'DISTANCE_SENSOR',
             'ESTIMATOR_STATUS', 'HIGHRES_IMU', 'RC_CHANNELS', 'SYS_STATUS')


def identity_of(record: dict) -> tuple:
    return (*record['peer'], record['sysid'], record['compid'])


def choose_clock(records: list[dict], types: tuple[str, ...], requested: str) -> tuple[str, float, str]:
    """Validate explicit boot fields per type, never align unknown time_usec."""
    selected = [r for r in records if r['msg_type'] in types]
    boot_ok = bool(selected)
    last: dict[str, float] = {}
    for record in selected:
        value = finite(record['fields'].get('time_boot_ms'))
        typ = record['msg_type']
        if value is None or value < 0 or value < last.get(typ, value):
            boot_ok = False
        if value is not None:
            last[typ] = value
    # Entire identity's common boot origin, not independent per-curve origins.
    boot_values = [finite(r['fields'].get('time_boot_ms')) for r in records]
    boot_values = [v / 1000 for v in boot_values if v is not None and v >= 0]
    if requested != 'receive' and boot_ok:
        return 'boot', min(boot_values), '飞控启动相对时间 (s)；显式 time_boot_ms，按类型未见倒退；未做跨源同步'
    origin = min((r['recv_monotonic_ns'] for r in records), default=0) / 1e9
    return 'receive', origin, '电脑单调接收相对时间 (s)；包含链路延迟，不支持毫秒级因果判断'


def control_context(records: list[dict], freshness: float = 2.0) -> Counter:
    """Prior heartbeat only; never carry mode/arming indefinitely through outages."""
    states: Counter = Counter()
    heartbeat = None
    for record in records:
        if record['msg_type'] == 'HEARTBEAT':
            heartbeat = record
        if record['msg_type'] != 'ATTITUDE_TARGET':
            continue
        if heartbeat is None or (record['recv_monotonic_ns'] - heartbeat['recv_monotonic_ns']) / 1e9 > freshness:
            states['状态未知/心跳过期'] += 1
            continue
        fields = heartbeat['fields']
        mode = fields.get('base_mode')
        custom = fields.get('custom_mode')
        if type(mode) is not int:
            states['状态未知'] += 1
        elif not mode & 128:
            states['未解锁：仅显示目标消息，不代表执行控制'] += 1
        elif fields.get('autopilot') == 12 and type(custom) is int and ((custom >> 16) & 255) in (2, 3, 4, 5, 6, 7, 8):
            states['解锁且相关PX4模式：仍不证明控制已执行'] += 1
        else:
            states['控制模式未确认：仅遥测目标'] += 1
    return states


def build_figures(data: SessionData, identity: tuple, *, time_axis: str = 'auto',
                  gap_seconds: float = 0.5) -> list[dict]:
    records = [r for r in data.records if identity_of(r) == identity]
    specs = [
        ('实际与目标姿态', ('ATTITUDE', 'ATTITUDE_TARGET'),
         [('ATTITUDE', 'roll', '实际 Roll', 1, 180/math.pi, None),
          ('ATTITUDE_TARGET', 'roll_deg', '遥测目标 Roll（不等于已执行）', 1, 1, None),
          ('ATTITUDE', 'pitch', '实际 Pitch', 2, 180/math.pi, None),
          ('ATTITUDE_TARGET', 'pitch_deg', '遥测目标 Pitch（不等于已执行）', 2, 1, None)], ['Roll (°)', 'Pitch (°)']),
        ('水平位置：EKF2估计，不是独立测量真值', ('LOCAL_POSITION_NED',),
         [('LOCAL_POSITION_NED', 'x', 'X North', 1, 1, None), ('LOCAL_POSITION_NED', 'y', 'Y East', 1, 1, None)], ['位置 (m)']),
        ('水平速度：直接使用消息估计，非位置差分', ('LOCAL_POSITION_NED',),
         [('LOCAL_POSITION_NED', 'vx', 'vx North', 1, 1, None), ('LOCAL_POSITION_NED', 'vy', 'vy East', 1, 1, None)], ['速度 (m/s)']),
    ]
    flow_traces = []
    for sensor in sorted({r['fields'].get('sensor_id') for r in records if r['msg_type'] == 'OPTICAL_FLOW_RAD'}, key=str):
        for field, label, row in [('integrated_x', 'integrated_x (rad)', 1), ('integrated_y', 'integrated_y (rad)', 1), ('quality', 'quality', 2), ('integration_time_us', 'integration_time_us', 3)]:
            flow_traces.append(('OPTICAL_FLOW_RAD', field, f'ID={sensor} {label}', row, 1, sensor))
    specs.append(('光流积分角量（不是水平速度）', ('OPTICAL_FLOW_RAD',), flow_traces, ['积分角 (rad)', '质量 (0–255)', '积分时间 (µs)']))
    distance_traces = []
    sensors = {(r['fields'].get('id'), r['fields'].get('orientation')) for r in records if r['msg_type'] == 'DISTANCE_SENSOR'}
    for sensor in sorted(sensors, key=str):
        for field in ('current_distance', 'min_distance', 'max_distance'):
            distance_traces.append(('DISTANCE_SENSOR', field, f'ID={sensor[0]} orientation={sensor[1]} {field}', 1, 1, sensor))
    specs.append(('测距：沿传感器方向，不自动等同离地高度', ('DISTANCE_SENSOR',), distance_traces, ['距离 (m)']))
    result = []
    for title, types, traces, units in specs:
        axis, origin, axis_label = choose_clock(records, types, time_axis)
        figure = make_subplots(rows=len(units), cols=1, shared_xaxes=True, vertical_spacing=0.08)
        quality = []
        for typ, field, label, row, scale, sensor in traces:
            trace_records = records
            if typ == 'OPTICAL_FLOW_RAD' and sensor is None:
                trace_records = [r for r in records if r['msg_type'] == typ and r['fields'].get('sensor_id') is None]
            values = series(trace_records, typ, field, scale=scale, time_axis=axis, origin=origin,
                            gap_seconds=gap_seconds, identity=identity, sensor=sensor)
            figure.add_trace(go.Scatter(x=values['x'], y=values['y'], name=label,
                                       mode='lines+markers', marker={'size': 3}, connectgaps=False,
                                       hovertemplate='%{x:.6f} s<br>%{y:.6g}<extra>%{fullData.name}</extra>'), row=row, col=1)
            quality.append({'trace': label, 'valid': values['valid_count'], 'invalid': values['invalid_count'], 'gaps': values['gap_count']})
        for row, unit in enumerate(units, 1):
            figure.update_yaxes(title_text=unit, row=row, col=1)
        figure.update_xaxes(title_text=axis_label, row=len(units), col=1)
        figure.update_layout(title=title, template='plotly_white', height=max(400, len(units)*240),
                             hovermode='x unified', legend={'orientation': 'h'}, margin={'b': 100})
        # Range reference lines alone are not a usable distance observation.
        relevant = [q for q in quality if 'min_distance' not in q['trace'] and 'max_distance' not in q['trace']]
        status = 'AVAILABLE_WITH_LIMITATIONS' if relevant and all(q['valid'] >= 2 for q in relevant) else 'INSUFFICIENT_DATA'
        note = '至少两条有效样本仅表示可以绘图，不是诊断可靠性或飞行健康判定。'
        if status == 'INSUFFICIENT_DATA':
            note += ' 缺少必要消息/有效字段或样本不足；优先补充MAVLink采集，ULog是否能补足须核对实际话题。'
        if types == ('ATTITUDE', 'ATTITUDE_TARGET'):
            note += ' 目标上下文：' + json.dumps(dict(control_context(records)), ensure_ascii=False)
        if axis == 'receive':
            note += ' 此图未确认统一飞控时间域或发现时间回退，回退到接收时间；不同图的时间原点不可直接比较。'
        result.append({'title': title, 'figure': figure, 'status': status, 'quality': quality, 'note': note, 'time_axis': axis})
    return result


def table(headers: list[str], rows: list[list]) -> str:
    return '<table><thead><tr>' + ''.join(f'<th>{escape(str(h))}</th>' for h in headers) + '</tr></thead><tbody>' + ''.join('<tr>' + ''.join(f'<td>{escape(str(c))}</td>' for c in row) + '</tr>' for row in rows) + '</tbody></table>'


def generate_report(session: Path, *, time_axis: str = 'auto', gap_seconds: float = 0.5) -> Path:
    if time_axis not in ('auto', 'receive'):
        raise ValueError('time_axis must be auto or receive')
    if not math.isfinite(gap_seconds) or gap_seconds <= 0:
        raise ValueError('gap_seconds must be finite and positive')
    data = load_session(session)
    parts = ['<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>PX4 离线诊断报告</title>',
             '<style>body{font:16px system-ui;max-width:1300px;margin:30px auto;padding:20px;color:#182438}table{border-collapse:collapse;width:100%;margin:15px 0}td,th{border:1px solid #ccd4dd;padding:7px;text-align:left}pre{white-space:pre-wrap;overflow-wrap:anywhere}.warning{background:#fff1d5;padding:15px}h2{margin-top:40px}</style></head><body>',
             '<h1>PX4 Flight Diagnostic Lab · 离线可视化</h1>',
             f'<p>Session: {escape(session.name)} · 生成UTC: {datetime.now(timezone.utc).isoformat()}</p>',
             '<p class="warning">只读 · MAVLink-first / ULog-on-demand。根因判断：INSUFFICIENT_DATA。估计位置不是独立真值；未解锁/模式未知的目标不代表飞行控制行为。CRC不阻止绘图，但降低证据完整性。</p>',
             f'<p>断线阈值：{gap_seconds:g}秒；超过阈值或无效值断线，不插值。auto按图选轴；需要全图统一接收时间比较时使用 --time-axis receive。</p>',
             '<h2>Session元信息</h2><pre>' + escape(json.dumps(data.metadata, ensure_ascii=False, indent=2)) + '</pre>']
    errors = Counter(r.get('error_type', 'unknown') for r in data.errors)
    parts.append('<h2>通信质量（processed记录重新统计，不需raw）</h2>')
    parts.append(table(['有效消息', '解析错误事件', 'CRC候选错误', '其他错误'], [[len(data.records), len(data.errors), errors['crc'], dict(errors)]]))
    parts.append('<p>错误事件不等于UDP丢包；无法可靠归属到某个SYSID的解析错误仅作为会话级指标。消息缺失不等于飞控没有产生。</p>')
    if data.summary:
        parts.append('<details><summary>采集时原始摘要（会话级，非单机归属）</summary><pre>' + escape(json.dumps(data.summary, ensure_ascii=False, indent=2)) + '</pre></details>')
    for warning in data.warnings:
        parts.append(f'<p class="warning">{escape(warning)}</p>')
    js_included = False
    if not data.identities:
        parts.append('<h2>INSUFFICIENT_DATA：没有有效消息，五组图表均缺少输入。</h2>')
    for identity in data.identities:
        records = [r for r in data.records if identity_of(r) == identity]
        parts.append(f'<h2>来源 {escape(str(identity[:2]))} · SYSID={identity[2]} COMPID={identity[3]}</h2>')
        rows = []
        for typ in KEY_TYPES:
            rr = [r for r in records if r['msg_type'] == typ]
            times = [r['recv_monotonic_ns']/1e9 for r in rr]
            span = times[-1]-times[0] if times else 0
            freq = f'{(len(times)-1)/span:.3f}' if span > 0 else '未知'
            gap = max((b-a for a,b in zip(times,times[1:])), default=None)
            rows.append([typ, len(rr), freq, f'{gap:.3f}' if gap is not None else '未知', '收到（字段另行校验）' if rr else 'INSUFFICIENT_DATA / 消息缺失'])
        parts.append(table(['消息', '数量', '接收Hz', '最大间隔s', '覆盖情况'], rows))
        for item in build_figures(data, identity, time_axis=time_axis, gap_seconds=gap_seconds):
            parts.append(f'<h3>{escape(item["title"])} · {item["status"]}</h3><p>{escape(item["note"])}</p>')
            parts.append(table(['曲线', '有效字段样本', '无效/缺失字段', '断线事件'], [[q['trace'],q['valid'],q['invalid'],q['gaps']] for q in item['quality']]))
            parts.append(item['figure'].to_html(full_html=False, include_plotlyjs=not js_included,
                                                config={'responsive': True, 'displaylogo': False, 'scrollZoom': True}))
            js_included = True
    parts.append('<h2>数据有效性与局限</h2><p>有效消息不保证所有字段有效。NaN、缺字段、忽略掩码、无效四元数不填零；测距按ID/方向隔离并检查量程。光流积分量不是速度；未使用未知time_usec做跨消息对齐。接收时间包含传输延迟，飞控启动字段检查不证明绝对同步，也不识别所有重启。未提供飞行根因、自动控制或ULog融合。长Session按内存加载，尚未做抽稀和大型日志性能保证。</p></body></html>')
    directory = session / 'reports'
    directory.mkdir(parents=True, exist_ok=True)
    # Exclusive open also protects reports created by concurrent processes.
    index = 0
    while True:
        target = directory / ('diagnostic.html' if index == 0 else f'diagnostic-{index:03d}.html')
        try:
            with target.open('x', encoding='utf-8') as handle:
                handle.write('\n'.join(parts))
            return target
        except FileExistsError:
            index += 1
