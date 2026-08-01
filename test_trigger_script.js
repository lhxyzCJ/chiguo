'use strict';
const assert = require('node:assert');

const SEND = { action: 'send', trigger: 'lonely_mid', intensity: 'medium',
               context: { instruction: '生成消息' }, msg_id: 'a1b2c3' };
const IDLE = { action: 'idle', reason: 'no_trigger', time: '2026-08-01T12:00:00+08:00' };
const BROKEN = '{"action": "send", broken';

let passed = 0;
function t(name, fn) { fn(); passed++; console.log(`  ok - ${name}`); }

const { parseDecision, decide } = require('./scripts/chiguo-watch.js');

t('parseDecision: 单行合法 JSON', () => {
  const d = parseDecision(JSON.stringify(SEND));
  assert.deepStrictEqual(d, SEND);
});
t('parseDecision: 空 stdout → null', () => {
  assert.strictEqual(parseDecision(''), null);
  assert.strictEqual(parseDecision('   \n\n'), null);
});
t('parseDecision: 损坏 JSON → null', () => {
  assert.strictEqual(parseDecision(BROKEN), null);
});
t('parseDecision: 多行缩进 JSON（daemon send 实际输出 indent=2）→ 解析成功', () => {
  const pretty = JSON.stringify(SEND, null, 2);
  assert.ok(pretty.includes('\n'), 'fixture 必须为多行');
  const d = parseDecision(pretty);
  assert.deepStrictEqual(d, SEND);
});
t('parseDecision: 前导空行 + 单行 JSON', () => {
  const d = parseDecision(`\n${JSON.stringify(IDLE)}\n`);
  assert.deepStrictEqual(d, IDLE);
});
t('parseDecision: 杂音行 + 单行 JSON（stdout 泄漏防护）→ 回退到 JSON 行', () => {
  const d = parseDecision(`some noise\n${JSON.stringify(IDLE)}`);
  assert.deepStrictEqual(d, IDLE);
});
t('decide: send → fire:true + 自包含 message', () => {
  const r = decide(SEND, {});
  assert.strictEqual(r.fire, true);
  assert.deepStrictEqual(JSON.parse(r.message), SEND);
  assert.strictEqual(r.state.last_error, undefined);
});
t('decide: idle → fire:false', () => {
  const r = decide(IDLE, {});
  assert.strictEqual(r.fire, false);
});
t('decide: null 决策（daemon 无输出）→ fire:false + last_error', () => {
  const r = decide(null, {});
  assert.strictEqual(r.fire, false);
  assert.match(r.state.last_error, /no decision/);
});
t('decide: 未知 action → fire:false + last_error', () => {
  const r = decide({ action: 'weird' }, {});
  assert.strictEqual(r.fire, false);
  assert.match(r.state.last_error, /unknown action/);
});
t('decide: idle 保留 prev state（last_error 清除）', () => {
  const r = decide(IDLE, { last_error: 'x', keep: 1 });
  assert.deepStrictEqual(r.state, { keep: 1 });
});
t('decide: send 清除 last_error', () => {
  const r = decide(SEND, { last_error: 'old' });
  assert.strictEqual(r.state.last_error, undefined);
});

(async () => {
  // ── run() 全链路（mock 全局 tools/json/trigger）──
  let captured = null;
  global.json = (r) => { captured = r; };
  global.tools = { call: async () => ({ result: { details: { aggregated: JSON.stringify(SEND) } } }) };
  global.trigger = { state: {} };
  delete require.cache[require.resolve('./scripts/chiguo-watch.js')];
  require('./scripts/chiguo-watch.js');
  await new Promise(r => setTimeout(r, 100));
  t('run(): mock exec 返回 send → fire:true', () => {
    assert.strictEqual(captured.fire, true);
    assert.deepStrictEqual(JSON.parse(captured.message), SEND);
  });

  captured = null;
  global.tools = { call: async () => ({ result: { details: { aggregated: '' } } }) };
  delete require.cache[require.resolve('./scripts/chiguo-watch.js')];
  require('./scripts/chiguo-watch.js');
  await new Promise(r => setTimeout(r, 100));
  t('run(): daemon 无输出 → fire:false + last_error', () => {
    assert.strictEqual(captured.fire, false);
    assert.match(captured.state.last_error, /no decision/);
  });

  captured = null;
  global.tools = { call: async () => { throw new Error('exec tool failed'); } };
  delete require.cache[require.resolve('./scripts/chiguo-watch.js')];
  require('./scripts/chiguo-watch.js');
  await new Promise(r => setTimeout(r, 100));
  t('run(): exec 抛错 → fire:false（不崩溃）', () => {
    assert.strictEqual(captured.fire, false);
    assert.match(captured.state.last_error, /exec tool failed/);
  });

  console.log(`test_trigger_script: ${passed}/15 passed`);
})().catch(e => { console.error('FAIL', e); process.exit(1); });
