'use strict';
// Display containment/reflection only; Python owns projection, subdivision and hit tests.
function calibrationRawPoint(x, y, box, width, height, mirrored) {
  const scale = Math.min(box.width / width, box.height / height);
  const left = box.left + (box.width - width * scale) / 2;
  const top = box.top + (box.height - height * scale) / 2;
  const px = (x - left) / scale, py = (y - top) / scale;
  if (px < 0 || py < 0 || px > width - 1 || py > height - 1) return null;
  return [mirrored ? width - 1 - px : px, py];
}

function calibrationRectangle(a, b, mirrored) {
  const left = mirrored ? Math.max(a[0], b[0]) : Math.min(a[0], b[0]);
  const right = mirrored ? Math.min(a[0], b[0]) : Math.max(a[0], b[0]);
  const top = Math.min(a[1], b[1]), bottom = Math.max(a[1], b[1]);
  return [[left, top], [right, top], [right, bottom], [left, bottom]];
}
if (typeof module !== 'undefined') module.exports = {calibrationRawPoint, calibrationRectangle};

if (typeof document !== 'undefined') {
  const el = id => document.getElementById(id);
  const dialog = el('calibrationDialog'), canvas = el('calibrationCanvas'), boardName = el('boardName');
  const labels = {chord:'选音区', strum:'拨弦区'}, colors = {chord:'#99e5bb', strum:'#f9c56d'};
  let session = null, regions = {chord:[], strum:[]}, active = 'chord', history = [], drag = null;
  let frozenImage = null, previewImage = null, previewToken = null, busy = false;
  let boardNameEdited = false;
  const copy = value => JSON.parse(JSON.stringify(value));
  const displayPoint = p => [session.mirrored ? canvas.width - 1 - p[0] : p[0], p[1]];
  const rawPoint = event => calibrationRawPoint(event.clientX, event.clientY, canvas.getBoundingClientRect(), canvas.width, canvas.height, session.mirrored);
  const remember = () => {history.push({regions:copy(regions), active}); if (history.length > 100) history.shift();};
  async function request(action, data = {}) {
    const r = await fetch('/api/calibration/' + action, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
    if (!r.ok) {const error = Error(await r.text()); error.status = r.status; throw error;}
    return r.json();
  }
  function draw() {
    const g = canvas.getContext('2d');
    g.clearRect(0, 0, canvas.width, canvas.height);
    if (frozenImage) g.drawImage(previewImage || frozenImage, 0, 0);
    if (session && !previewImage) for (const key of ['chord', 'strum']) {
      const points = regions[key].map(displayPoint);
      if (!points.length) continue;
      g.strokeStyle = colors[key]; g.fillStyle = colors[key]; g.lineWidth = key === active ? 3 : 2;
      g.beginPath(); points.forEach(([x, y], i) => i ? g.lineTo(x, y) : g.moveTo(x, y));
      if (points.length === 4) {g.closePath(); g.globalAlpha = .12; g.fill(); g.globalAlpha = 1;}
      g.stroke();
      points.forEach(([x, y], i) => {
        g.beginPath(); g.arc(x, y, 6, 0, 2*Math.PI); g.fill();
        g.font = 'bold 16px sans-serif'; g.fillText(String(i+1), x+10, y+18);
      });
      g.font = 'bold 18px sans-serif'; g.fillText(labels[key], points[0][0]+10, points[0][1]-10);
    }
    el('calibrationUndo').disabled = busy || !history.length;
    el('calibrationReset').disabled = busy || !regions[active].length;
    el('calibrationPreview').disabled = busy || !!drag || !session || Object.values(regions).some(p => p.length !== 4);
    el('calibrationSave').disabled = busy || !previewToken;
    for (const id of ['calibrationCapture', 'calibrationCancel', 'boardName', 'calibrationMode', 'selectChordRegion', 'selectStrumRegion']) el(id).disabled = busy;
    el('selectChordRegion').setAttribute('aria-pressed', String(active === 'chord'));
    el('selectStrumRegion').setAttribute('aria-pressed', String(active === 'strum'));
    el('calibrationCount').textContent = session ? `选音区 ${regions.chord.length}/4 · 拨弦区 ${regions.strum.length}/4 · 冻结帧 F${session.frame_id}` : '请先捕获一帧';
    el('calibrationInstruction').textContent = `当前：${labels[active]}。` + (el('calibrationMode').value === 'rectangle' ? '拖出矩形；拖动已有角点可调整形状。' : '按画面中的左上 → 右上 → 右下 → 左下点选；拖动已有角点可调整。');
  }
  function changed() {previewToken = previewImage = null; el('calibrationError').textContent = ''; draw();}
  function advance() {if (active === 'chord' && regions.chord.length === 4 && regions.strum.length === 0) active = 'strum';}
  async function loadImage(base64) {
    const image = new Image(); image.src = 'data:image/jpeg;base64,' + base64; await image.decode(); return image;
  }
  async function work(fn) {
    if (busy || drag) return;
    busy = true; el('calibrationError').textContent = ''; draw();
    try {await fn();} catch (e) {el('calibrationError').textContent = e.message;}
    finally {busy = false; draw();}
  }
  el('calibrate').onclick = () => {dialog.showModal(); draw();};
  el('calibrationCapture').onclick = () => work(async () => {
    const captured = await request('capture');
    if (!boardNameEdited && captured.suggested_board_id) boardName.value = captured.suggested_board_id;
    session = captured; regions = {chord:[], strum:[]}; active = 'chord'; history = [];
    frozenImage = previewImage = previewToken = null;
    canvas.width = captured.frame_width; canvas.height = captured.frame_height;
    frozenImage = await loadImage(captured.image_base64);
  });
  for (const [id, key] of [['selectChordRegion', 'chord'], ['selectStrumRegion', 'strum']]) el(id).onclick = () => {active = key; draw();};
  el('calibrationMode').onchange = draw;
  canvas.onpointerdown = event => {
    if (busy || drag || !session || !frozenImage || (event.pointerType === 'mouse' && event.button !== 0)) return;
    const point = rawPoint(event);
    if (!point) return;
    const box = canvas.getBoundingClientRect(), scale = Math.min(box.width/canvas.width, box.height/canvas.height);
    const index = regions[active].findIndex(p => Math.hypot(p[0]-point[0], p[1]-point[1])*scale < 14);
    if (index < 0 && el('calibrationMode').value === 'corners') {
      if (regions[active].length === 4) return;
      remember(); regions[active].push(point); advance(); changed(); return;
    }
    drag = {id:event.pointerId, start:point, index, before:copy(regions), active};
    canvas.setPointerCapture(event.pointerId); changed();
  };
  canvas.onpointermove = event => {
    if (!drag || event.pointerId !== drag.id) return;
    const point = rawPoint(event);
    if (!point) return;
    if (drag.index >= 0) regions[active][drag.index] = point;
    else regions[active] = calibrationRectangle(drag.start, point, session.mirrored);
    draw();
  };
  function finishDrag(event, cancel) {
    if (!drag || event.pointerId !== drag.id) return;
    const previous = drag; drag = null;
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
    const points = regions[active];
    if (cancel || points.length !== 4 || points.some((p, i) => Math.hypot(p[0]-points[(i+1)%4][0], p[1]-points[(i+1)%4][1]) < 2)) regions = previous.before;
    else {history.push({regions:previous.before, active:previous.active}); advance();}
    changed();
  }
  canvas.onpointerup = event => {canvas.onpointermove(event); finishDrag(event, false);};
  canvas.onpointercancel = event => finishDrag(event, true);
  canvas.onlostpointercapture = event => finishDrag(event, true);
  el('calibrationUndo').onclick = () => {const previous = history.pop(); if (previous) {regions = previous.regions; active = previous.active;} changed();};
  el('calibrationReset').onclick = () => {remember(); regions[active] = []; changed();};
  boardName.oninput = () => {boardNameEdited = true; changed();};
  el('calibrationPreview').onclick = () => work(async () => {
    previewToken = previewImage = null;
    const data = await request('preview', {session_id:session.session_id, regions_raw:regions, board_id:boardName.value});
    previewImage = await loadImage(data.image_base64); previewToken = data.preview_token;
    el('calibrationError').textContent = '预览有效：请检查七个选音格与拨弦中线，再确认保存。';
  });
  el('calibrationSave').onclick = () => work(async () => {
    const data = await request('save', {session_id:session.session_id, preview_token:previewToken});
    boardNameEdited = false;
    clear(); dialog.close();
    el('calibrationSaved').hidden = false;
    el('calibrationSaved').textContent = `已保存并启用板面 ${data.saved} 与区域 ${data.preset_saved}。重启时使用 --board ${data.saved} 即可恢复。`;
  });
  function clear() {session = null; regions = {chord:[], strum:[]}; history = []; active = 'chord'; frozenImage = previewImage = previewToken = null;}
  async function cancel() {
    if (session) {
      try {await request('cancel', {session_id:session.session_id});}
      catch (e) {if (e.status !== 400) throw e;} // Expired/replaced sessions can be dismissed locally.
    }
    clear(); dialog.close();
  }
  el('calibrationCancel').onclick = () => work(cancel);
  dialog.addEventListener('cancel', event => {event.preventDefault(); if (!busy) work(cancel);});
}
