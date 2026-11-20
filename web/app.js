'use strict';
const $ = id => document.getElementById(id);
let latest = null, stopped = false, updating = false;
let recordingPreferenceSet = false;
let performanceBusy = false, expectedPerformance = null;
const prettyNote = value => String(value).replace(/#/g, '♯').replace(/b/g, '♭');

function awaitingPerformance(packet) {
  return expectedPerformance !== null && packet.service_instance_id === expectedPerformance.service_instance_id &&
    (!packet.performance || packet.performance.revision < expectedPerformance.revision);
}

function renderPerformance(packet) {
  const config = packet?.performance;
  if (!config) return;
  const disabled = performanceBusy || packet.status !== 'running' || packet.calibrating ||
    $('calibrationDialog').open || awaitingPerformance(packet);
  const options = (config.tonics || []).map(t=>'<option value="'+escapeText(t)+'">'+prettyNote(t)+'</option>').join('');
  if ($('tonic').innerHTML !== options) $('tonic').innerHTML = options;
  if (!performanceBusy) $('tonic').value = config.tonic;
  for (const [id, mode] of [['modeChord','chord'],['modeMelody','melody']]) {
    $(id).setAttribute('aria-pressed', String(config.mode === mode));
    $(id).classList.toggle('secondary', config.mode !== mode);
    $(id).disabled = disabled;
  }
  $('tonic').disabled = disabled;
  $('performanceHint').textContent = prettyNote(config.tonic)+' 大调 · '+(config.mode === 'chord' ? '七级三和弦' : '七个单音')+' · 左手选音，右手扫弦';
}

async function changePerformance(mode, tonic) {
  if (performanceBusy || !latest?.performance) return;
  performanceBusy = true;
  $('performanceError').textContent = '';
  renderPerformance(latest);
  try {
    const r = await fetch('/api/performance', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode,tonic}), signal:AbortSignal.timeout(3000)});
    if (!r.ok) throw Error(await r.text());
    expectedPerformance = await r.json();
    // Wait for the matching packet/image before updating selection or region labels.
  } catch (e) {$('performanceError').textContent = e.message;}
  finally {performanceBusy = false; renderPerformance(latest);}
}
const fmt = (n, digits = 1) => Number.isFinite(n) ? n.toFixed(digits) : '—';
const escapeText = text => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function clearLive(message) {
  latest = null;
  $('status').textContent = message;
  $('video').removeAttribute('src');
  $('waiting').hidden = false;
  $('waitMessage').textContent = message;
  $('chord').textContent = '—';
  $('chordState').textContent = '当前数据不可用';
  $('hands').textContent = '无新鲜手部数据';
  $('nodes').replaceChildren();
  $('world').getContext('2d').clearRect(0, 0, 800, 300);
  $('fps').textContent = '—';
  $('event').textContent = '—';
  $('boardState').textContent = 'UNAVAILABLE';
  $('markerState').textContent = '等待标记';
  $('frameInfo').textContent = '无新鲜画面';
  $('audioState').textContent = '状态不可用';
  $('audioDetail').textContent = '等待新的服务状态';
  $('eventDetail').textContent = '历史事件见下方记录';
  $('strumState').textContent = '等待新鲜画面';
  $('strumDetail').textContent = '右手食指尖的 STRUM 圆圈用于拨弦';
  $('selectHand').replaceChildren();
  $('record').disabled = true;
  for (const id of ['modeChord','modeMelody','tonic']) $(id).disabled = true;
  for (const id of ['latency', 'arucoMs', 'totalMs']) $(id).textContent = '—';
  $('chordStrip').querySelectorAll('.active').forEach(node => node.classList.remove('active'));
}

function drawWorld(data) {
  const g = $('world').getContext('2d');
  g.clearRect(0, 0, 800, 300);
  g.font = '14px monospace'; g.fillStyle = '#a8b5ab';
  g.fillText('XY / LOCAL', 20, 25); g.fillText('XZ / LOCAL', 420, 25);
  for (const hand of data.hands) {
    g.strokeStyle = g.fillStyle = hand.handedness === 'Left' ? '#58d6b0' : '#f3ae4e';
    const offset = hand.handedness === 'Left' ? -65 : 65;
    for (let view = 0; view < 2; view++) {
      const pts = hand.landmarks_world_m.map(p => [200 + 400*view + offset + p[0]*1100, 165 + p[view === 0 ? 1 : 2]*1100]);
      g.lineWidth = 2;
      for (const [a, b] of data.connections) {g.beginPath(); g.moveTo(...pts[a]); g.lineTo(...pts[b]); g.stroke();}
      for (const p of pts) {g.beginPath(); g.arc(...p, 3, 0, Math.PI*2); g.fill();}
    }
  }
}

function renderTable() {
  if (!latest) return;
  const hand = latest.hands.find(h => String(h.track_id) === $('selectHand').value) || latest.hands[0];
  $('nodes').innerHTML = hand ? hand.landmarks_px.map((p, i) => {
    const uv = hand.landmarks_instrument_uv?.[i];
    const values = [fmt(p[0]), fmt(p[1]), fmt(p[2]), fmt(hand.landmarks_normalized[i][0], 4), fmt(hand.landmarks_normalized[i][1], 4), fmt(uv?.[0], 4), fmt(uv?.[1], 4)];
    return '<tr><td>'+i+'</td><td>'+escapeText(latest.landmark_names[i])+'</td>'+values.map(v=>'<td>'+v+'</td>').join('')+'</tr>';
  }).join('') : '<tr><td colspan="9">当前无手部节点</td></tr>';
}

function chordMessage(data) {
  const state = data.interaction || {};
  if (state.selection_valid) return '已选中 · 右手食指可扫弦';
  const reason = state.selection_reason || state.reason;
  const messages = {
    board_or_service_unavailable: '板面定位未就绪 · 请让至少两枚标记清晰可见',
    calibration_in_progress: '正在标定板面 · 保存或取消后恢复选择',
    layout_requires_calibration: '旧布局缺少完整外框 · 请重新划定选音区',
    performance_changed: '演奏设置已切换 · 请重新停留选音',
    stale_or_out_of_order_frame: '画面延迟过高 · 等待新鲜画面',
    ambiguous_hand_identity: '左右手识别冲突 · 请展开双手，避免重叠',
    hand_identity_changed: '手部识别切换 · 请稍作停留',
    left_hand_missing: '未识别到左手 · 绿色骨架选音，橙色骨架扫弦',
    left_hand_projection_unavailable: '左手位置暂不可用 · 请保持板面标记可见',
    left_hand_unreliable: '左手识别不稳定 · 请露出完整手掌和手指',
    outside_chord_regions: '选择点在选音区外 · 将 SELECT 圆圈移入绿色区域',
    chord_boundary: '选择点靠近边界 · 请移向选音区中央',
  };
  const message = reason === 'dwelling'
    ? '候选 '+prettyNote(state.candidate_label)+' · 保持片刻 '+Math.round((state.selection_dwell_progress || 0)*100)+'%'
    : messages[reason] || '等待左手食指选择';
  return (state.selected_label ? '上次选择 · ' : '')+message;
}

function renderStrum(data) {
  const state = data.interaction || {};
  const messages = {
    board_or_service_unavailable:'板面定位不可用 · 请露出标记',
    calibration_in_progress:'标定中 · 暂停拨弦',
    camera_unavailable:'摄像头暂不可用',
    performance_changed:'设置已切换 · 请重新选音和扫弦',
    layout_requires_calibration:'请先划定选音区和拨弦区',
    stale_or_out_of_order_frame:'画面延迟 · 等待新鲜画面',
    ambiguous_hand_identity:'左右手识别冲突',
    hand_identity_changed:'手部身份切换 · 请重新扫弦',
    selection_not_ready:'左手还未选定音',
    right_hand_missing:'未识别到右手',
    right_hand_unreliable:'右手识别不稳定',
    outside_strum_zone:'右手食指离拨弦区过远',
    invalid_region_projection:'拨弦坐标暂不可用',
    strum_identity_changed:'右手跟踪变化 · 请重新扫弦',
    strum_motion_outlier:'右手坐标跳变 · 已忽略',
    strum_missed_axis:'轨迹从中线端点外经过',
    strum_too_slow:'跨线速度不足',
    strum_cooldown:'短暂冷却 · 避免重复发声',
    strum_triggered:'已触发拨弦',
    strum_waiting_crossing:'已准备 · 请跨过中线',
    strum_waiting_start_side:'先把食指放到中线一侧',
  };
  $('strumState').textContent = messages[state.strum_reason] || '等待右手扫弦';
  const side = {above:'上侧',below:'下侧',crossing:'中线缓冲带',unarmed:'尚未准备'}[state.strum_state] || '尚未准备';
  $('strumDetail').textContent = side+' · '+(state.strum_point_uv ? (state.strum_in_region ? '指尖在框内' : '指尖在框外') : '未取得指尖位置')+' · 左手保持所选区间';
}

function renderRecording(data) {
  $('record').textContent = data.recording ? '停止录制' : '开始录制';
  $('record').disabled = data.status !== 'running' && !data.recording;
  $('recordVideo').disabled = !!data.recording;
  if (!recordingPreferenceSet && data.status === 'running') {
    $('recordVideo').checked = !!data.recording_default_raw_frames;
    recordingPreferenceSet = true;
  }
  $('recording').textContent = data.recording ? '● 正在录制'+(data.recording_details?.raw_frames ? '原始图像 + 数据' : '节点数据')+'：'+data.last_recording : data.last_recording ? '录制目录：'+data.last_recording : '默认仅录制节点。勾选实验采集可同步保存原始图像。';
}

function render(data) {
  const running = data.status === 'running';
  latest = data;
  const board = data.instrument || {}, interaction = data.interaction || {}, audio = data.audio || {};
  renderPerformance(data);
  renderStrum(data);
  $('status').textContent = running ? '● 本地服务运行中' : data.status === 'error' ? '服务错误' : data.status === 'stopped' ? '服务已停止' : '启动中';
  $('cameraName').textContent = data.camera_name || '等待摄像头';
  $('simulation').hidden = data.source !== 'synthetic';
  $('fps').textContent = data.fps ?? '—';
  $('latency').textContent = fmt(data.inference_ms);
  $('arucoMs').textContent = fmt(board.aruco_ms);
  $('totalMs').textContent = fmt(data.vision_total_ms);
  $('chord').textContent = interaction.selected_label ? interaction.selected_degree+' · '+prettyNote(interaction.selected_label) : '—';
  $('chordState').textContent = chordMessage(data);
  $('boardState').textContent = ({valid:'VALID · 已定位', degraded:'DEGRADED · 暂停演奏', held:'HELD · 暂停演奏', lost:'LOST · 等待标记'})[board.status] || '等待定位';
  $('markerState').textContent = (board.pose_quality?.visible_marker_count ?? 0)+' / '+(board.required_ids?.length ?? 0)+' MARKERS · IDs '+(board.visible_ids || []).join(', ');
  $('qualityReason').textContent = board.reason || (board.board_id ? '板面：'+board.board_id : '');
  $('poseQuality').textContent = JSON.stringify(board.pose_quality || {}, null, 2);
  $('audioState').textContent = ({ready:'输出流就绪', mock:'MOCK · 无实际声音', disabled:'已关闭', error:'ERROR · 输出不可用', closed:'已关闭'})[data.audio_status] || '初始化中';
  $('audioDetail').textContent = audio.error || (audio.backend || '—')+' · 已消费 '+(audio.played_events || 0)+' · 队列 '+(audio.queue_depth || 0)+' · 丢弃 '+(audio.dropped_events || 0);
  const recent = data.recent_events || [], event = recent.at(-1);
  $('event').textContent = event ? (event.direction === 'down' ? '↓ 下扫' : '↑ 上扫')+' · '+prettyNote(event.label || event.chord) : '—';
  $('eventDetail').textContent = event ? '#'+event.event_id+' · velocity '+event.velocity+' · frame '+event.source_frame_id : '等待新事件';
  $('events').innerHTML = recent.length ? recent.slice(-6).reverse().map(e=>'<li><span>#'+e.event_id+' '+(e.direction === 'down' ? '↓' : '↑')+' '+escapeText(prettyNote(e.label || e.chord))+' · '+(e.mode === 'melody' ? '单音' : '和弦')+'</span><span>VEL '+e.velocity+' / F'+e.source_frame_id+'</span></li>').join('') : '<li>尚无扫弦事件</li>';
  const voices = data.performance?.voices || [];
  $('chordStrip').innerHTML = voices.map(v=>'<div class="chord-chip '+(interaction.selection_valid && v.degree === interaction.selected_degree ? 'active' : '')+'">'+escapeText(prettyNote(v.label))+'<small>'+v.degree+' 级</small></div>').join('');
  $('hands').textContent = data.hands.map(h=>(h.handedness === 'Left' ? '左手 / 选音' : '右手 / 扫弦')+' #'+h.track_id+' · '+fmt(h.confidence*100, 0)+'%').join('　') || '等待双手进入画面';
  $('error').textContent = [data.error, audio.error, data.recording_error, data.layout_error].filter(Boolean).join(' · ');
  renderRecording(data);
  $('frameInfo').textContent = 'F'+(data.frame_id || 0)+' · '+(data.frame_width || 0)+'×'+(data.frame_height || 0)+' · '+(data.mirrored ? '镜像' : '原始');
  const old = $('selectHand').value;
  const options = data.hands.map(h=>'<option value="'+h.track_id+'">'+h.handedness+' #'+h.track_id+'</option>').join('');
  if ($('selectHand').innerHTML !== options) $('selectHand').innerHTML = options;
  if (data.hands.some(h=>String(h.track_id) === old)) $('selectHand').value = old;
  if ($('debug').open) {renderTable(); drawWorld(data);}
}

async function update() {
  if (stopped || updating) return;
  updating = true;
  try {
    const response = await fetch('/api/snapshot', {signal:AbortSignal.timeout(3000)});
    if (!response.ok) throw Error('HTTP '+response.status);
    const {packet, image_base64:image} = await response.json();
    if (stopped) return;
    if (packet.status === 'running' && Date.now()/1000-packet.timestamp_unix_s > 1) throw Error('画面已过期');
    if (image && packet.status === 'running') {
      if (awaitingPerformance(packet)) return;
      // One response carries one immutable packet/JPEG pair. No JS geometry or inference.
      const next = new Image();
      next.src = 'data:image/jpeg;base64,'+image;
      await next.decode();
      if (stopped || awaitingPerformance(packet)) return;
      $('video').src = next.src;
      $('waiting').hidden = true;
      render(packet);
    } else {
      render(packet);
      clearLive(packet.error || (packet.status === 'stopped' ? '服务已停止，可重新运行启动脚本。' : '摄像头启动中…'));
    }
  } catch (error) {
    if (!stopped) {clearLive('本地服务未连接或画面已过期'); $('error').textContent = error.message;}
  } finally {
    updating = false;
    if (!stopped) setTimeout(update, 80);
  }
}

$('selectHand').onchange = renderTable;
$('modeChord').onclick = () => changePerformance('chord', latest.performance.tonic);
$('modeMelody').onclick = () => changePerformance('melody', latest.performance.tonic);
$('tonic').onchange = () => changePerformance(latest.performance.mode, $('tonic').value);
$('debug').ontoggle = () => {if (latest && $('debug').open) {renderTable(); drawWorld(latest);}};
$('record').onclick = async () => {
  try {
    $('record').disabled = true;
    const r = await fetch('/api/record/'+(latest?.recording ? 'stop' : 'start'), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({raw_frames:$('recordVideo').checked})});
    if (!r.ok) throw Error(await r.text());
    const state = await r.json();
    if (latest && latest.service_instance_id === state.service_instance_id) {
      latest = {...latest, recording:state.recording};
      renderRecording(state);
    }
  } catch (e) {$('error').textContent = e.message; $('record').disabled = false;}
};
$('stop').onclick = async () => {
  try {
    const r = await fetch('/api/shutdown', {method:'POST'});
    if (!r.ok) throw Error(await r.text());
    const state = await r.json();
    stopped = true; clearLive('服务已停止，可重新运行启动脚本。');
    $('recording').textContent = state.last_recording ? '录制结束：'+state.last_recording : '未录制';
    $('record').disabled = $('stop').disabled = true;
    $('audioState').textContent = '已停止';
  } catch (e) {$('error').textContent = e.message;}
};
update();
