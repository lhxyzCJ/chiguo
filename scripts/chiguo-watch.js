#!/usr/bin/env node
// chiguo-watch.js — OpenClaw automations trigger script（迟菓主动消息）
// 官方契约: docs.openclaw.ai/automation/cron-jobs → "Event triggers"
// 职责: 无模型执行 daemon --compact; 仅 action=send 时 fire 唤醒 agent。
// 容错: daemon 崩溃/坏 JSON/超时 → fire:false + state.last_error（下次评估带出）。
'use strict';

const path = require('node:path');

function parseDecision(stdout) {
  const text = String(stdout ?? '').trim();
  if (!text) return null;
  // 优先级: 全文（daemon send 输出为 indent=2 多行 JSON）→ 逐行回退（stdout 杂音泄漏防护）
  const candidates = [text, ...text.split('\n').map(l => l.trim()).filter(Boolean)];
  for (const c of candidates) {
    try {
      const obj = JSON.parse(c);
      if (obj && typeof obj === 'object') return obj;
    } catch { /* try next candidate */ }
  }
  return null;
}

function decide(decision, prevState) {
  const state = { ...(prevState && typeof prevState === 'object' ? prevState : {}) };
  if (!decision) {
    state.last_error = 'no decision JSON on stdout';
    return { fire: false, state };
  }
  if (decision.action === 'send') {
    delete state.last_error;
    return { fire: true, message: JSON.stringify(decision), state };
  }
  if (decision.action === 'idle') {
    delete state.last_error;
    return { fire: false, state };
  }
  state.last_error = `unknown action: ${decision.action}`;
  return { fire: false, state };
}

async function run() {
  const repo = String(process.env.CHIGUO_REPO || path.resolve(__dirname, '..')).replace(/\/+$/, '');
  const command = `${repo}/.venv/bin/python ${repo}/chiguo_daemon.py --compact`;
  const res = await tools.call('exec', { command });
  const aggregated = String(res?.result?.details?.aggregated ?? '').trim();
  const prevState = typeof trigger !== 'undefined' && trigger.state;
  json(decide(parseDecision(aggregated), prevState));
}

// OpenClaw 执行器提供 tools/json/trigger 全局 → 执行主流程；
// node 测试环境（无这些全局）→ 导出纯函数。
if (typeof tools !== 'undefined' && typeof json !== 'undefined') {
  run().catch(err => json({ fire: false, state: { last_error: String(err && err.message || err) } }));
} else if (typeof module !== 'undefined') {
  module.exports = { parseDecision, decide };
}
