"""app/actions/executor.py — Action 执行器（Phase 6, #477）。

send_message 的送达闭环：
  pending action → started
  → 生成（默认：scripts/agent-run.mjs --send-mode，由 Pi 按人格实现 intent）
  → 发送（默认：bridge /send）
  → message.sent / message.delivery_failed 事件 + delivery 行 + action 终态。

生成与发送均可注入（测试零网络）。生成失败 → action.failed 事件（无送达）；
发送失败 → delivery.failed + message.delivery_failed（对账/重试的依据）。
"""
import json
import os
import subprocess
from dataclasses import dataclass
from datetime import datetime

from chiguo_paths import PROJECT_ROOT
from chiguo_time import CST
from storage.events import EventStore
from storage.repositories.actions import ActionRepo, DeliveryRepo
from storage.repositories.drives import IntentRepo
from storage.repositories.messages import MessageRepo
from storage.sqlite.db import Database

GENERATE_TIMEOUT_S = 180.0
SEND_TIMEOUT_S = 35.0


@dataclass(frozen=True)
class ExecOutcome:
    ok: bool
    status: str          # sent / generate_failed / send_failed / invalid
    error: str = ""
    text: str | None = None


def build_generation_payload(intent, *, config: dict, now: datetime | None = None) -> dict:
    """Intent → agent-run send-mode 可消费的决策 JSON（体裁兼容既有模板）。

    context.layer_guidance/instruction 为 agent-run buildSendPrompt 的既有消费字段；
    v2 的 intent/why/plan 一并放入 context，供 Pi 侧（Phase 7 extension）使用。
    """
    now = now or datetime.now(CST)
    why = getattr(intent, "why", None) or {}
    plan = getattr(intent, "plan", None) or {}
    intent_type = getattr(intent, "type", "share")
    return {
        "action": "send",
        "version": "v2",
        "msg_id": getattr(intent, "id", None),
        "trigger": intent_type,        # 兼容字段（旧模板/遥测）
        "intensity": "soft",
        "context": {
            "intent_type": intent_type,
            "instruction": (
                "请以迟菓（personality/迟菓人格-精简版.md 设定）的身份，"
                "用上述语气发一条微信消息给哥哥。1-3句话。自然。"
            ),
            "layer_guidance": (
                f"[v2 意图] {intent_type}；依据: {json.dumps(why, ensure_ascii=False)}"
            ),
            "situation": f"[计划] {json.dumps(plan, ensure_ascii=False)}",
        },
        "state": _state_summary(config, now),
    }


def _state_summary(config: dict, now: datetime) -> dict:
    """最小 state 摘要（课表/时间）；完整世界状态注入由 Phase 7 extension 承担。"""
    return {"time": now.strftime("%Y-%m-%d %H:%M"),
            "character": (config.get("character", {}) or {}).get("name", "迟菓")}


# ── 默认实现（生产路径）───────────────────────────────────────

def default_generator(prompt_json: str, config: dict) -> str | None:
    """node scripts/agent-run.mjs --prompt <payload> --send-mode → 文本。"""
    script = PROJECT_ROOT / "scripts" / "agent-run.mjs"
    env = {**os.environ, "CHIGUO_REPO": str(PROJECT_ROOT)}
    p = subprocess.run(
        ["node", str(script), "--prompt", prompt_json, "--send-mode"],
        capture_output=True, text=True, timeout=GENERATE_TIMEOUT_S, env=env)
    try:
        data = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return data.get("text") if data.get("ok") and data.get("text") else None


def default_sender(text: str, config: dict) -> dict:
    """bridge /send（复用 ops.bridge_ops：token 注入 + 回环代理绕过）。"""
    from ops.bridge_ops import bridge_post
    to = (config.get("wechat", {}) or {}).get("wechat_recipient", "")
    if not to:
        return {"ok": False, "error": "wechat_recipient not configured"}
    bridge_url = str((config.get("loop", {}) or {}).get(
        "bridge_url", "http://127.0.0.1:18790")).rstrip("/")
    token = os.environ.get("WECHAT_BRIDGE_TOKEN") or \
        str((config.get("loop", {}) or {}).get("bridge_token", "") or "")
    return bridge_post(bridge_url, token, "/send", {"to": to, "text": text},
                       SEND_TIMEOUT_S)


# ── 执行 ─────────────────────────────────────────────────────

def execute_action(db: Database, action_id: str, *, config: dict,
                   now: datetime | None = None, generator=None,
                   sender=None) -> ExecOutcome:
    now = now or datetime.now(CST)
    repo = ActionRepo(db)
    action = repo.get(action_id)
    if action is None or action.type != "send_message":
        return ExecOutcome(False, "invalid", f"unknown send action: {action_id}")
    if action.status != "pending":
        return ExecOutcome(False, "invalid", f"action not pending: {action.status}")

    repo.set_status(action_id, "started")
    intent = IntentRepo(db).get(action.intent_id) if action.intent_id else None
    correlation = action.correlation_id or action_id
    store = EventStore(db)

    payload = build_generation_payload(intent, config=config, now=now)
    gen = generator or default_generator
    try:
        text = gen(json.dumps(payload, ensure_ascii=False), config)
    except Exception as e:  # noqa: BLE001 —— 生成异常按失败处理（可重试）
        text = None
        err = f"generate exception: {e}"
    else:
        err = "generate failed: 空回复"

    if not text:
        repo.set_status(action_id, "failed", error=err)
        store.append("action.failed", source="executor",
                     occurred_at=now, payload={"action_id": action_id,
                                               "error": err},
                     correlation_id=correlation,
                     causation_id=action.cause_event_id)
        return ExecOutcome(False, "generate_failed", err)

    drepo = DeliveryRepo(db)
    delivery = drepo.add(action_id, channel="wechat", status="pending")
    send = sender or default_sender
    try:
        resp = send(text, config) or {}
    except Exception as e:  # noqa: BLE001
        resp = {"ok": False, "error": f"send exception: {e}"}

    if resp.get("ok"):
        drepo.set_status(delivery.id, "sent")
        MessageRepo(db).add(direction="out", text=text, at=now)
        ev = store.append("message.sent", source="wechat", occurred_at=now,
                          payload={"text": text,
                                   "intent_id": action.intent_id},
                          correlation_id=correlation,
                          causation_id=action.cause_event_id)
        repo.set_status(action_id, "completed",
                        output={"text": text, "event_id": ev.event_id})
        return ExecOutcome(True, "sent", "", text=text)

    error = str(resp.get("error") or "send failed")
    drepo.set_status(delivery.id, "failed", error=error)
    store.append("message.delivery_failed", source="wechat", occurred_at=now,
                 payload={"error": error, "intent_id": action.intent_id},
                 correlation_id=correlation, causation_id=action.cause_event_id)
    repo.set_status(action_id, "failed", error=error)
    return ExecOutcome(False, "send_failed", error)
