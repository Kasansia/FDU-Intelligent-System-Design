const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const script = fs.readFileSync(path.join(__dirname, '../web/app.js'), 'utf8');
const settings = (instance, revision, mode = 'chord', tonic = 'C') => ({
  service_instance_id: instance, revision, mode, tonic, tonics: ['C', 'D'],
  voices: [{degree: 1, label: mode === 'melody' ? tonic + '4' : tonic}],
});

function snapshot(instance, revision, frameId, options = {}) {
  const {status = 'running', captured = Date.now()/1000, mode, error} = options;
  const {service_instance_id, ...performance} = settings(instance, revision, mode);
  return {
    packet: {
      status, error, frame_id: frameId, timestamp_unix_s: captured, hands: [],
      service_instance_id, performance,
    },
    image_base64: status === 'running' ? instance + '-' + frameId : null,
  };
}

async function dashboard() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      elements.set(id, {
        attributes: new Map(),
        classList: {toggle() {}},
        setAttribute(key, value) {this.attributes.set(key, value);},
        removeAttribute(key) {delete this[key];},
        replaceChildren() {},
        querySelectorAll() {return [];},
        getContext() {return {clearRect() {}};},
      });
    }
    return elements.get(id);
  };
  const replies = [];
  const enqueue = (path, data, ok = true) => replies.push({path, data, ok});
  const sandbox = vm.createContext({
    document: {getElementById: element},
    Image: class {async decode() {}},
    AbortSignal,
    setTimeout() {},
    fetch: async path => {
      // Use a plain reply so the test exercises the production fetch handlers.
      const reply = replies.shift();
      assert.equal(reply?.path, path);
      return {ok: reply.ok, json: async () => reply.data, text: async () => reply.data};
    },
  });
  enqueue('/api/snapshot', snapshot('first', 0, 41));
  vm.runInContext(script, sandbox);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(element('video').src, 'data:image/jpeg;base64,first-41');
  const poll = async data => {
    enqueue('/api/snapshot', data);
    await vm.runInContext('update()', sandbox);
    assert.equal(replies.length, 0);
  };
  const switchToMelody = async () => {
    enqueue('/api/performance', settings('first', 1, 'melody'));
    await element('modeMelody').onclick();
    assert.equal(element('tonic').disabled, true);
  };
  return {element, enqueue, poll, switchToMelody};
}

test('live frames and controls update together after a performance change', async () => {
  const {element, poll, switchToMelody} = await dashboard();
  await switchToMelody();
  await poll(snapshot('first', 0, 42));
  assert.equal(element('video').src, 'data:image/jpeg;base64,first-41');
  assert.equal(element('modeChord').attributes.get('aria-pressed'), 'true');
  await poll(snapshot('first', 1, 43, {mode: 'melody'}));
  assert.equal(element('video').src, 'data:image/jpeg;base64,first-43');
  assert.equal(element('modeMelody').attributes.get('aria-pressed'), 'true');
  assert.equal(element('tonic').disabled, false);
});

test('a pending change does not hide errors, stops, or expired frames', async () => {
  for (const [options, message] of [
    [{status: 'error', error: 'Camera read failed'}, /Camera read failed/],
    [{status: 'stopped'}, /服务已停止/],
    [{captured: Date.now()/1000 - 2}, /未连接或画面已过期/],
  ]) {
    const {element, poll, switchToMelody} = await dashboard();
    await switchToMelody();
    await poll(snapshot('first', 0, 42, options));
    assert.equal(element('video').src, undefined);
    assert.equal(element('waiting').hidden, false);
    assert.equal(element('tonic').disabled, true);
    assert.match(element('status').textContent, message);
  }
});

test('a restarted service can render revision zero while a change is pending', async () => {
  const {element, poll, switchToMelody} = await dashboard();
  await switchToMelody();
  await poll(snapshot('second', 0, 1));
  assert.equal(element('video').src, 'data:image/jpeg;base64,second-1');
  assert.equal(element('modeChord').attributes.get('aria-pressed'), 'true');
  assert.equal(element('tonic').disabled, false);
});

test('a rejected settings request restores editability and the live tonic', async () => {
  const {element, enqueue} = await dashboard();
  enqueue('/api/performance', 'Performance service not ready', false);
  element('tonic').value = 'D';
  await element('tonic').onchange();
  assert.equal(element('performanceError').textContent, 'Performance service not ready');
  assert.equal(element('tonic').value, 'C');
  assert.equal(element('tonic').disabled, false);
});

test('a recording response updates recording status without replacing live frame data', async () => {
  const {element, enqueue, poll} = await dashboard();
  const newFrame = snapshot('first', 1, 42, {mode: 'melody'});
  newFrame.packet.recording = true;
  enqueue('/api/record/start', newFrame.packet);
  await element('record').onclick();
  assert.equal(element('record').textContent, '停止录制');
  assert.equal(element('video').src, 'data:image/jpeg;base64,first-41');
  assert.equal(element('modeChord').attributes.get('aria-pressed'), 'true');
  assert.match(element('frameInfo').textContent, /^F41 /);
  await poll(newFrame);
  assert.equal(element('video').src, 'data:image/jpeg;base64,first-42');
  assert.equal(element('modeMelody').attributes.get('aria-pressed'), 'true');
});
