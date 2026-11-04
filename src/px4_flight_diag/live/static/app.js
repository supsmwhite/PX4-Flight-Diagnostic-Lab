/* Read-only version-1 telemetry viewer. Values are already converted by telemetry. */
(() => {
  'use strict';
  const WINDOW = 60, MAX_POINTS = 12000, MAX_TRACES = 128;
  const groups = ['attitude', 'position', 'velocity', 'flow', 'distance'];
  const $ = id => document.getElementById(id);
  const text = (id, value) => { $(id).textContent = value; };
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  const display = value => value == null ? '未知' : typeof value === 'object' ? JSON.stringify(value) : String(value);
  const number = value => finite(value) ? value.toFixed(2) : '未知';
  const identityKey = identity => JSON.stringify(identity);
  const validIdentity = identity => Array.isArray(identity) && identity.length === 4;
  const traces = new Map();
  const plots = Object.fromEntries(groups.map(name => [name, { element: $('plot-' + name), keys: [], dirty: true, ready: false }]));
  const debug = window.__liveDebug = { updates: 0, pointCount: 0, state: 'WAITING_FOR_DATA', connected: false, plots: {} };
  let snapshot = null, session = null, selected = '', highId = -1, latestT = 0;
  let socket, reconnectTimer, retry = 500, receivedAt = 0, rendering = false;
  let localDropped = 0, localError = '', renderTick = 0;
  const contexts = new Map();
  const specs = {
    ATTITUDE: [['attitude', 'roll', '实际 Roll', 'y'], ['attitude', 'pitch', '实际 Pitch', 'y2']],
    ATTITUDE_TARGET: [['attitude', 'roll', '遥测目标 Roll（不等于执行）', 'y'], ['attitude', 'pitch', '遥测目标 Pitch（不等于执行）', 'y2']],
    LOCAL_POSITION_NED: [['position', 'x', 'X North', 'y'], ['position', 'y', 'Y East', 'y'], ['velocity', 'vx', 'vx North', 'y'], ['velocity', 'vy', 'vy East', 'y']],
    OPTICAL_FLOW_RAD: [['flow', 'integrated_x', 'integrated_x', 'y'], ['flow', 'integrated_y', 'integrated_y', 'y'], ['flow', 'quality', 'quality', 'y2'], ['flow', 'integration_time_us', 'integration_time_us', 'y3']],
    DISTANCE_SENSOR: [['distance', 'current_distance', 'current_distance', 'y'], ['distance', 'min_distance', 'min_distance', 'y'], ['distance', 'max_distance', 'max_distance', 'y']]
  };
  function reset(nextSession) {
    session = nextSession; traces.clear(); contexts.clear(); selected = ''; highId = -1; latestT = 0; localDropped = 0;
    for (const plot of Object.values(plots)) plot.dirty = true;
  }
  function addPoint(point) {
    if (!point || !Number.isSafeInteger(point.id) || point.id < 0) { localError = '协议错误：point.id 必须为非负安全整数'; return; }
    if (point.id <= highId) return; // Session-global monotonic ids bound deduplication memory.
    highId = point.id;
    if (!finite(point.t) || !validIdentity(point.identity) || !point.values || typeof point.values !== 'object') {
      localDropped++; localError = '协议错误：point 缺少有效 t / identity / values'; return;
    }
    latestT = Math.max(latestT, point.t);
    if (point.t < latestT - WINDOW) return;
    const source = identityKey(point.identity);
    if (point.target_context != null && (contexts.has(source) || contexts.size < MAX_TRACES)) contexts.set(source, point.target_context);
    for (const [group, field, label, axis] of specs[point.msg_type] || []) {
      const key = JSON.stringify([point.identity, point.msg_type, point.sensor ?? null, field]);
      let trace = traces.get(key);
      if (!trace) {
        if (traces.size >= MAX_TRACES) { localDropped++; continue; }
        const sensor = point.msg_type === 'DISTANCE_SENSOR' || point.msg_type === 'OPTICAL_FLOW_RAD';
        // Plotly supports HTML in labels: use only static labels and numeric sensor identifiers.
        const sensorParts = Array.isArray(point.sensor) ? point.sensor : [point.sensor];
        const sensorLabel = sensorParts.map(v => Number.isSafeInteger(v) ? String(v) : '?').join('/');
        trace = { key, group, source, field, x: [], y: [], pendingX: [], pendingY: [], lastT: null,
          name: label + (sensor ? ' · sensor ' + sensorLabel : ''), axis, target: point.msg_type === 'ATTITUDE_TARGET' };
        traces.set(key, trace); plots[group].dirty = true;
      }
      if (trace.lastT !== null && (point.t - trace.lastT > 0.5 || point.t < trace.lastT)) append(trace, point.t, null);
      append(trace, point.t, finite(point.values[field]) ? point.values[field] : null);
      trace.lastT = point.t;
    }
  }
  function append(trace, x, y) {
    trace.x.push(x); trace.y.push(y); trace.pendingX.push(x); trace.pendingY.push(y);
    if (trace.x.length > MAX_POINTS) {
      const excess = trace.x.length - MAX_POINTS;
      trace.x.splice(0, excess); trace.y.splice(0, excess); localDropped += excess;
    }
    if (trace.pendingX.length > MAX_POINTS) {
      trace.pendingX.splice(0, trace.pendingX.length - MAX_POINTS);
      trace.pendingY.splice(0, trace.pendingY.length - MAX_POINTS);
      plots[trace.group].dirty = true;
    }
  }
  function ingest(value) {
    if (!value || value.version !== 1 || !Array.isArray(value.points) || !Array.isArray(value.sources)) {
      localError = '协议错误：需要 version=1 的 snapshot（points/sources 数组）'; return;
    }
    if (session !== value.session) reset(value.session);
    snapshot = value; receivedAt = Date.now(); localError = '';
    // No client requests or cursor messages: the server sends its bounded history on reconnect.
    for (const point of value.points) addPoint(point);
  }
  function updateStatus() {
    const elapsed = receivedAt ? (Date.now() - receivedAt) / 1000 : 0;
    const connected = socket?.readyState === WebSocket.OPEN;
    debug.connected = connected;
    $('connection').dataset.connected = String(connected);
    text('connection', connected ? '浏览器已连接 · 同源只读 WebSocket' : '浏览器断线 · 自动重连中；所示为历史数据');
    const terminal = snapshot && (snapshot.finished || ['COMPLETED', 'ERROR'].includes(snapshot.state));
    let state = snapshot?.state || 'WAITING_FOR_DATA';
    if (!connected) state = 'DISCONNECTED';
    else if (elapsed > 2 && !terminal) state = 'STALE';
    debug.state = state;
    text('state', state + (snapshot?.state === 'COMPLETED' ? ' · 采集结束（保留末60秒）' : '') + (elapsed > 2 && !terminal ? ' · 快照过期' : ''));
    text('session', 'Session：' + display(session));
    text('mode', '模式：' + display(snapshot?.mode));
    text('messages', '消息：' + display(snapshot?.messages));
    text('crc', 'CRC候选错误：' + display(snapshot?.crc_errors) + ' · 序号候选缺口：' + display(snapshot?.sequence_candidate_missing));
    text('dropped', '服务端显示丢弃：' + display(snapshot?.display_dropped) + ' · 客户端漏收：' + display(snapshot?.client_missed) + ' · 浏览器容量丢弃：' + localDropped);
    const age = value => number(finite(value) ? value + (terminal ? 0 : elapsed) : null);
    text('ages', 'UDP链路：' + display(snapshot?.link_state) + ' · 数据 age：' + age(snapshot?.last_data_age_s) + ' s · 消息 age：' + age(snapshot?.last_message_age_s) + ' s' + (terminal ? '（采集末状态）' : ''));
    text('updated', receivedAt ? '最后快照：' + new Date(receivedAt).toLocaleString() + ' · ' + elapsed.toFixed(1) + ' s 前 · cursor ' + display(snapshot?.cursor) : '最后快照：尚未收到');
    text('error', [snapshot?.error && display(snapshot.error), ...(snapshot?.warnings || []), snapshot?.conversion_errors ? '字段转换错误：' + snapshot.conversion_errors : '', localError].filter(Boolean).join('\n'));
    const sources = (snapshot?.sources || []).filter(source => validIdentity(source.identity));
    const optionsKey = JSON.stringify(sources.map(source => source.identity));
    if ($('identity').dataset.options !== optionsKey) {
      $('identity').replaceChildren();
      for (const source of sources) {
        const option = document.createElement('option'); option.value = identityKey(source.identity);
        option.textContent = source.identity.map(display).join(' / '); $('identity').append(option);
      }
      $('identity').dataset.options = optionsKey;
      if (!sources.some(source => identityKey(source.identity) === selected)) {
        selected = sources.length ? identityKey(sources[0].identity) : '';
        for (const plot of Object.values(plots)) plot.dirty = true;
      }
      $('identity').value = selected;
    }
    const source = sources.find(item => identityKey(item.identity) === selected);
    text('heartbeat', 'HEARTBEAT：' + display(source?.heartbeat_state) + ' · age ' + age(source?.heartbeat_age_s) + ' s' + (!connected || elapsed > 2 ? ' · 当前新鲜度不可确认' : ''));
    text('target-context', '目标上下文：' + display(contexts.get(selected) ?? snapshot?.target_context) + '；仅遥测，不等于执行' + (!connected || elapsed > 2 ? '（历史上下文）' : ''));
    const rows = document.createDocumentFragment();
    for (const stream of source?.streams || []) {
      const row = document.createElement('tr');
      for (const cell of [stream.msg_type, stream.count, number(stream.frequency_hz), number(stream.max_gap_s), age(stream.age_s), (!connected || elapsed > 2) ? display(stream.state) + '（历史快照）' : stream.state]) {
        const td = document.createElement('td'); td.textContent = display(cell); row.append(td);
      }
      rows.append(row);
    }
    $('streams').replaceChildren(rows);
  }
  function layout(group) {
    const base = { margin: { l: 70, r: 25, t: 20, b: 85 }, paper_bgcolor: '#fff', plot_bgcolor: '#fff',
      uirevision: session + selected, showlegend: true, legend: { orientation: 'h', y: -0.22 },
      xaxis: { title: { text: '电脑接收相对秒（含链路延迟）' }, range: [Math.max(0, latestT - WINDOW), Math.max(1, latestT)] },
      yaxis: { title: { text: { position: '位置 (m)', velocity: '速度 (m/s)', distance: '距离 (m)', attitude: 'Roll (°)', flow: '积分角 (rad)' }[group] } } };
    if (group === 'attitude') {
      base.yaxis.domain = [0.57, 1]; base.yaxis2 = { domain: [0, 0.43], title: { text: 'Pitch (°)' }, anchor: 'x' };
    }
    if (group === 'flow') {
      base.yaxis.domain = [0.72, 1];
      base.yaxis2 = { domain: [0.37, 0.63], title: { text: '质量 (0–255)' }, anchor: 'x' };
      base.yaxis3 = { domain: [0, 0.26], title: { text: '积分时间 (µs)' }, anchor: 'x' };
    }
    return base;
  }
  function plotTrace(trace) {
    return { type: 'scatter', mode: 'lines+markers', marker: { size: 3 }, line: { dash: trace.target ? 'dash' : 'solid' },
      name: trace.name, x: trace.x.slice(), y: trace.y.slice(), yaxis: trace.axis, connectgaps: false,
      hovertemplate: '%{x:.3f} s<br>%{y:.6g}<extra>%{fullData.name}</extra>' };
  }
  async function renderPlots() {
    if (rendering || !window.Plotly) return;
    rendering = true;
    try {
      // Only one render may run; ingest uses bounded arrays while Plotly promises resolve.
      renderTick++;
      for (const [key, trace] of traces) {
        let cut = 0;
        while (cut < trace.x.length && trace.x[cut] < latestT - WINDOW) cut++;
        if (cut) { trace.x.splice(0, cut); trace.y.splice(0, cut); }
        // Canonical memory trims every tick; reconcile Plotly storage once per second.
        if (renderTick % 5 === 0 && latestT > WINDOW) plots[trace.group].dirty = true;
        if (!trace.x.length) { traces.delete(key); plots[trace.group].dirty = true; }
      }
      for (const group of groups) {
        const plot = plots[group];
        const visible = [...traces.values()].filter(trace => trace.group === group && trace.source === selected);
        const keys = visible.map(trace => trace.key);
        const full = plot.dirty || !plot.ready || JSON.stringify(keys) !== JSON.stringify(plot.keys);
        const updates = visible.map(trace => ({ x: trace.pendingX.splice(0), y: trace.pendingY.splice(0) }));
        plot.dirty = false;
        if (full) {
          await Plotly.react(plot.element, visible.map(plotTrace), layout(group), { responsive: true, displaylogo: false, scrollZoom: true });
          plot.keys = keys; plot.ready = true;
        } else if (updates.some(update => update.x.length)) {
          await Plotly.extendTraces(plot.element, { x: updates.map(update => update.x), y: updates.map(update => update.y) }, visible.map((_, i) => i), MAX_POINTS);
          await Plotly.relayout(plot.element, { 'xaxis.range': [Math.max(0, latestT - WINDOW), Math.max(1, latestT)] });
        }
        const observed = visible.filter(trace => !['min_distance', 'max_distance'].includes(trace.field));
        const enough = observed.length && observed.every(trace => trace.y.filter(finite).length >= 2);
        text('status-' + group, enough ? 'AVAILABLE_WITH_LIMITATIONS · 可绘图不等于健康或诊断可靠' : 'INSUFFICIENT_DATA · 缺少有效字段或至少两条有效样本');
        debug.plots[group] = { traces: visible.length, points: visible.reduce((sum, trace) => sum + trace.x.length, 0), status: enough ? 'AVAILABLE_WITH_LIMITATIONS' : 'INSUFFICIENT_DATA' };
      }
      // Nonselected sources retain only their bounded canonical arrays, never a hidden pending backlog.
      for (const trace of traces.values()) if (trace.source !== selected) { trace.pendingX.length = 0; trace.pendingY.length = 0; }
      debug.pointCount = [...traces.values()].reduce((sum, trace) => sum + trace.x.length, 0);
      debug.updates++;
    } catch (error) { localError = '绘图错误：' + display(error.message); }
    finally { rendering = false; }
  }
  $('identity').addEventListener('change', () => {
    selected = $('identity').value;
    for (const plot of Object.values(plots)) plot.dirty = true;
  });
  function connect() {
    clearTimeout(reconnectTimer);
    const endpoint = new URL('/ws', window.location.href);
    endpoint.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    socket = new WebSocket(endpoint);
    socket.addEventListener('open', () => { retry = 500; updateStatus(); });
    socket.addEventListener('message', event => {
      try { ingest(JSON.parse(event.data)); } catch (error) { localError = '快照解析错误：' + display(error.message); }
    });
    socket.addEventListener('error', () => { localError = 'WebSocket连接错误；等待自动重连'; });
    socket.addEventListener('close', () => {
      updateStatus(); reconnectTimer = setTimeout(connect, retry); retry = Math.min(retry * 2, 10000);
    });
  }
  setInterval(() => { updateStatus(); void renderPlots(); }, 200);
  if (!window.Plotly) localError = 'Plotly加载失败：请检查本机 /plotly.min.js';
  connect();
})();
