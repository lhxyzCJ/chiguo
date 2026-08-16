#!/usr/bin/env python3
"""test_loop_send.py — C1 daemon --loop 发送侧内聚单元测试（TDD）+ U2 发送侧可靠性（#227）"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine
from runner.loop import run_loop
import runner.loop as _loop_mod


class FakeBridge(BaseHTTPRequestHandler):
    """fake bridge：记录 /agent/prompt 与 /send 请求，响应可切换。"""

    requests = []  # (path, body)
    prompt_mode = "ok"   # ok | empty | error
    send_mode = "ok"     # ok=成功 | fail=/send 返回 ok:false（F-A6-2 发送失败域）| timeout_uncertain（F-A17-003）

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n).decode("utf-8") if n else ""
        FakeBridge.requests.append((self.path, body))
        if self.path == "/agent/prompt":
            if FakeBridge.prompt_mode == "error":
                self.send_response(503)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False, "error": "mock 故障"}).encode())
                return
            if FakeBridge.prompt_mode == "empty":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "text": "loop RPC 消息"}).encode())
            return
        if self.path == "/send":
            self.send_response(200)
            self.end_headers()
            if FakeBridge.send_mode == "fail":
                self.wfile.write(json.dumps({"ok": False, "error": "mock send 故障"}).encode())
            elif FakeBridge.send_mode == "timeout_uncertain":
                self.wfile.write(json.dumps(
                    {"ok": False, "error": "timeout 30000ms", "timeout_uncertain": True}).encode())
            else:
                self.wfile.write(json.dumps({"ok": True}).encode())
            return
        self.send_response(405)
        self.end_headers()
        self.wfile.write(b'{"ok":false}')

    def log_message(self, *a):  # noqa: ARG002
        pass


def _start_bridge():
    FakeBridge.requests = []
    FakeBridge.prompt_mode = "ok"
    FakeBridge.send_mode = "ok"
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def _make_engine(temp_dir: str) -> DecisionEngine:
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return DecisionEngine(str(cfg_path), str(Path(temp_dir) / "chiguo_decisions.jsonl"))


def _send_decision(msg_id="m1", trigger="lonely_mid", intensity="medium"):
    return {"action": "send", "msg_id": msg_id, "trigger": trigger,
            "intensity": intensity, "version": "1.11", "context": {"layer": "shell"}}


def _use_health_script():
    """Point _record_health subprocess at the repo's real agent_health.py.
    --state defaults to <base_dir>/agent_health.json (isolated under temp dir)."""
    os.environ["AGENT_HEALTH_SCRIPT"] = str(Path("scripts/agent_health.py").resolve())


def _fake_runner_retry(script_path):
    """Run-once-fail / run-twice-ok fake agent-run (switched by call-count file)."""
    counter = script_path.with_suffix(".cnt")
    counter.write_text("0")  # 预置计数起点，避免首次 readFileSync ENOENT
    script_path.write_text(
        "import { readFileSync, writeFileSync } from 'node:fs'\n"
        "const F=" + repr(str(counter)) + "\n"
        "const c=parseInt(readFileSync(F,'utf8').trim()||'0')+1\n"
        "writeFileSync(F,String(c))\n"
        "process.stdout.write(c===1?JSON.stringify({ok:false,error:'first fail'}):"
        "JSON.stringify({ok:true,text:'retry 兜底消息'}))\n"
    )
    return counter


def _restore_env(old_r, ah):
    if old_r is None:
        os.environ.pop("AGENT_RUN_SCRIPT", None)
    else:
        os.environ["AGENT_RUN_SCRIPT"] = old_r
    if ah is None:
        os.environ.pop("AGENT_HEALTH_SCRIPT", None)
    else:
        os.environ["AGENT_HEALTH_SCRIPT"] = ah


def test_loop_send_rpc_ok():
    """RPC 成功：/agent/prompt 带 mode=send → /send 带 to/text → 记账。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td)
            engine.config["wechat"]["wechat_recipient"] = "real_openid@im.wechat"
            loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}"}
            out = engine._loop_send(_send_decision(), loop_cfg)
            assert out["generated"] is True and out["sent"] is True, out
            paths = [r[0] for r in FakeBridge.requests]
            assert "/agent/prompt" in paths and "/send" in paths, paths
            prompt_body = json.loads([b for p, b in FakeBridge.requests if p == "/agent/prompt"][0])
            assert prompt_body["mode"] == "send", prompt_body
            assert json.loads(prompt_body["text"])["msg_id"] == "m1"
            send_body = json.loads([b for p, b in FakeBridge.requests if p == "/send"][0])
            assert send_body["to"] == "real_openid@im.wechat", send_body
            assert send_body["text"] == "loop RPC 消息", send_body
            # 记账：messages log 含 direction=send
            msgs = Path(td, "chiguo_messages.jsonl").read_text()
            assert '"direction": "send"' in msgs and "loop RPC 消息" in msgs, msgs
    finally:
        srv.shutdown()


def test_loop_send_rpc_fail_fallback_spawn():
    """RPC 失败（503）→ 回退 spawn agent-run（fake AGENT_RUN_SCRIPT）→ 发送成功。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-run.mjs"
            fake_runner.write_text(
                "process.stdout.write(JSON.stringify({ ok: true, text: 'spawn 兜底消息' }))")
            old = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "error"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                out = engine._loop_send(_send_decision(), loop_cfg)
                assert out["generated"] is True and out["sent"] is True, out
                send_body = json.loads(
                    [b for p, b in FakeBridge.requests if p == "/send"][0])
                assert send_body["text"] == "spawn 兜底消息", send_body
            finally:
                _restore_env(old, ah)
    finally:
        srv.shutdown()


def test_loop_send_all_fail():
    """U2 (#227): RPC 空回复 + spawn 失败（整链重试一次仍失败）→ generated=false +
    error 非空 + 无 composer 兜底 + fail_streak 推进（agent_health --state 临时文件）。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-run.mjs"
            fake_runner.write_text(
                "process.stdout.write(JSON.stringify({ ok: false, error: 'fake 故障' }))")
            old = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "empty"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                out = engine._loop_send(_send_decision(), loop_cfg)
                assert out["generated"] is False, out
                assert out.get("error"), out
                assert not out.get("sent"), out
                # 无 composer 兜底：无 /send（既不发消息也不发兜底文本）
                assert not any(p == "/send" for p, _ in FakeBridge.requests)
                # fail_streak 推进（仅重试也失败才计一次真故障）
                st = json.loads(Path(td, "agent_health.json").read_text())
                assert st.get("fail_streak") == 1, st
                assert st.get("state") == "up", st
            finally:
                _restore_env(old, ah)
    finally:
        srv.shutdown()


def test_loop_send_send_failure_refund():
    """发送失败 → sent=false + record_send_result failed（refund 闭环）。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td)
            engine.config["wechat"]["wechat_recipient"] = ""  # 空收件人 → 不发送
            loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                        "retry_delay_seconds": 0}
            out = engine._loop_send(_send_decision("m2"), loop_cfg)
            assert out["generated"] is True and out["sent"] is False, out
    finally:
        srv.shutdown()


def test_loop_send_send_failure_health_send_fail():
    """F-A6-2: /send 返回 ok:false → 发送失败走 refund 之外，还记 agent_health
    send_fail（推进 fail_streak；连续 3 次 → down + transition 告警不再恒 up）。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "owner@w"
                FakeBridge.send_mode = "fail"   # /send 恒返回 ok:false
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                # 第 1 次发送失败：refund + send_fail 记账（未达阈值，up）
                out1 = engine._loop_send(_send_decision("m-sf1"), loop_cfg)
                assert out1["generated"] is True and out1["sent"] is False, out1
                st1 = json.loads(Path(td, "agent_health.json").read_text())
                assert st1["state"] == "up", st1
                assert st1["fail_streak"] == 1, st1
                # 第 2、3 次 → 达阈值 down
                engine._loop_send(_send_decision("m-sf2"), loop_cfg)
                engine._loop_send(_send_decision("m-sf3"), loop_cfg)
                st3 = json.loads(Path(td, "agent_health.json").read_text())
                assert st3["state"] == "down", st3
                assert st3["fail_streak"] == 3, st3
                assert "send failed" in st3.get("fail_reason", ""), st3
                # 恰好 1 条 down transition 告警经 /send
                send_texts = [json.loads(b).get("text", "")
                              for p, b in FakeBridge.requests if p == "/send"]
                alert_texts = [t for t in send_texts if "后端异常" in t]
                assert len(alert_texts) == 1, send_texts
            finally:
                FakeBridge.send_mode = "ok"
    finally:
        srv.shutdown()


def test_loop_send_retry_success_no_fail():
    """U2 (#227): 首次生成失败 → sleep(retry_delay) 整链重试一次成功 → generated/sent True，
    不计 fail（agent_health 无 fail 记录，fail_streak 保持 0/up）。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-retry.mjs"
            counter = _fake_runner_retry(fake_runner)
            old_r = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "empty"  # RPC 恒空 → 走 spawn 重试链
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                out = engine._loop_send(_send_decision("m-retry"), loop_cfg)
                assert out["generated"] is True and out["sent"] is True, out
                send_texts = [json.loads(b)["text"]
                              for p, b in FakeBridge.requests if p == "/send"]
                assert send_texts == ["retry 兜底消息"], send_texts
                # 两次尝试（首次 fail + 重试成功）→ 无 fail 记账
                assert Path(counter).read_text().strip() == "2", "应整链重试一次"
                st = json.loads(Path(td, "agent_health.json").read_text())
                assert st.get("fail_streak") == 0, st
                assert st.get("state") == "up", st
            finally:
                _restore_env(old_r, ah)
    finally:
        srv.shutdown()


def test_loop_send_3_fail_down_alert():
    """U2 (#227): 连续 3 次真实失败（含整链重试）→ state=down + 恰好 1 条 transition 告警经 /send 发出。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-fail.mjs"
            fake_runner.write_text(
                "process.stdout.write(JSON.stringify({ ok: false, error: '持续故障' }))")
            old_r = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "empty"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                for _ in range(3):
                    engine._loop_send(_send_decision(), loop_cfg)
                st = json.loads(Path(td, "agent_health.json").read_text())
                assert st.get("state") == "down", st
                assert st.get("fail_streak") == 3, st
                # transition 唯一告警（down 只发一次）；正文含次数与原因
                send_texts = [json.loads(b).get("text", "")
                              for p, b in FakeBridge.requests if p == "/send"]
                alert_texts = [t for t in send_texts if "后端异常" in t]
                assert len(alert_texts) == 1, send_texts
                assert "持续故障" in alert_texts[0], alert_texts
            finally:
                _restore_env(old_r, ah)
    finally:
        srv.shutdown()


def test_loop_health_probe_rhythm():
    """U2 (#227): 降频探测节奏——累计失败(1<=streak<threshold)距上次 <1h 跳过、≥1h probe；
    健康/无状态放行；进程内首次 probe 恒放行（重启即恢复）。"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        health = Path(td, "agent_health.json")

        # 首次 probe（_loop_first_probe）恒放行
        assert engine._health_should_probe({"health_state": str(health)}) is True
        # 无状态文件 → 放行
        assert engine._health_should_probe({"health_state": str(health)}) is True

        health.write_text(json.dumps({"state": "up", "fail_streak": 1,
                                      "last_fail_at": datetime.now().astimezone().isoformat()}))
        assert engine._health_should_probe({"health_state": str(health)}) is False, "1h 内应跳过"

        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        health.write_text(json.dumps({"state": "up", "fail_streak": 2, "last_fail_at": old}))
        assert engine._health_should_probe({"health_state": str(health)}) is True, "≥1h 应 probe"

        health.write_text(json.dumps({"state": "down", "fail_streak": 3, "last_fail_at": old}))
        assert engine._health_should_probe({"health_state": str(health)}) is False, "down 应暂停"


def test_loop_health_restart_recovers():
    """U2 (#227): 重启恢复——down 状态下重新实例化（_loop_first_probe 复位）→ 首次 probe 放行 → 成功 → up + 恢复消息。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-ok.mjs"
            fake_runner.write_text(
                "process.stdout.write(JSON.stringify({ ok: true, text: '恢复后消息' }))")
            old_r = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            _use_health_script()
            try:
                health = Path(td, "agent_health.json")
                health.write_text(json.dumps({"state": "down", "fail_streak": 3,
                                              "fail_reason": "历史故障"}))
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "ok"  # RPC 直接成功
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "health_state": str(health), "retry_delay_seconds": 0}
                # 重启后首次 probe 恒放行（即使状态仍 down）
                assert engine._health_should_probe(loop_cfg) is True, "重启后首次应放行"
                out = engine._loop_send(_send_decision("m-restart"), loop_cfg)
                assert out["generated"] is True and out["sent"] is True, out
                st = json.loads(health.read_text())
                assert st.get("state") == "up", st
                assert st.get("fail_streak") == 0, st
                send_texts = [json.loads(b).get("text", "")
                              for p, b in FakeBridge.requests if p == "/send"]
                assert any("恢复" in t for t in send_texts), send_texts
                assert any("loop RPC 消息" == t for t in send_texts), send_texts  # RPC 成功 → 真实消息
            finally:
                _restore_env(old_r, ah)
    finally:
        srv.shutdown()


def test_alerts_push_wechat_chain():
    """Q24 (#275): 告警微信推送链路——collect_new_alerts_to_push 识别新增 critical/warn
    → _push_alerts_via_wechat 经 bridge /send 送达（mock bridge，不真发微信）。"""
    from chiguo_monitor import ChiguoMonitor, AlertManager, collect_new_alerts_to_push
    from chiguo_daemon import _push_alerts_via_wechat
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td)
            engine.config["wechat"]["wechat_recipient"] = "alert_owner@im.wechat"
            engine.config.setdefault("loop", {})
            engine.config["loop"]["bridge_url"] = f"http://127.0.0.1:{srv.server_port}"

            # 构造一个 no_state（critical）告警：state 文件缺失
            none_state = Path(td) / "none_state.json"
            log = Path(td) / "decisions.jsonl"
            log.write_text("")
            cfg = Path(td) / "chiguo_proactive.toml"
            cfg.write_text("[monitor]\n")
            mon = ChiguoMonitor(str(log), str(none_state), config_path=str(cfg))
            am = AlertManager(str(Path(td) / "chiguo_alerts.json"))

            FakeBridge.requests = []  # 计数前清零
            new_alerts = collect_new_alerts_to_push(mon, am)
            assert any(a["type"] == "no_state" for a in new_alerts), new_alerts
            pushed = _push_alerts_via_wechat(engine, new_alerts)
            assert len(pushed) == len(new_alerts), f"应全部投递: {pushed}"

            sends = [json.loads(b) for p, b in FakeBridge.requests if p == "/send"]
            assert len(sends) == 1, f"应恰好 1 次 /send: {sends}"
            assert sends[0]["to"] == "alert_owner@im.wechat", sends[0]
            assert "🚨" in sends[0]["text"], sends[0]["text"]
            assert "no_state" in sends[0]["text"] or "告警" in sends[0]["text"], sends[0]["text"]

            # 第二次运行：no_state 已活跃 → 不重推、不再发 /send
            again = collect_new_alerts_to_push(mon, am)
            assert again == [], f"重复运行不应重推: {again}"
    finally:
        srv.shutdown()


# ═══════════════════════════════════════════════════════════
# R7 (F-RT-001): suppressed 分支退款 —— run_loop.run() 在 health=down（或降频区间）
# 抑制发送后，必须对 evaluate 已记账的 send 决策调用 record_send_result(failed)，
# 回滚逃生阀冷却/额度，避免幻影记账；否则逃生阀 last_longing_break_at 被白扣。
# ═══════════════════════════════════════════════════════════

def _escape_valve_engine(temp_dir: str) -> DecisionEngine:
    """构造会出 escape_valve send 决策的引擎（复刻 test_escape_valve 端到端死锁态）。"""
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    _escape_valve_isolate_toml(cfg_path, Path(temp_dir))
    engine = DecisionEngine(str(cfg_path), str(Path(temp_dir) / "decisions.jsonl"))
    engine.config["schedule"]["quiet_start"] = 0
    engine.config["schedule"]["quiet_end"] = 0
    engine.state._sync_quiet_window()
    st = engine.state
    st.emotion.anxiety = 100.0  # 阻塞态
    # 4 天前交互 → 沉默 >72h 死锁态（v7: 从未交互不触发逃生阀，故不用 None）
    st.cooldown.last_user_message_at = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=4)).isoformat()
    st.cooldown.messages_today = 5  # 超日限额 → 逃生阀放行
    st.cooldown.last_message_at = (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=2)).isoformat()
    st.cooldown.current_date = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    st.cooldown.event_timestamps = []
    # 消除时段敏感：伪造非 sleeping 用户态（真实 infer 在 00-08 点会报 sleeping 高置信 → sleeping_guard）
    st.infer_user_state = lambda now=None, msg_length=None: {
        "posterior": {"sleeping": 0.1, "browsing": 0.8, "busy": 0.1},
        "most_likely": "browsing", "confidence": 0.3, "utility": 0.1,
        "should_send_bayesian": True, "state_description": "browsing",
    }
    return engine


def _escape_valve_isolate_toml(cfg_path: Path, tmp: Path) -> None:
    """隔离真实 toml 的记忆库路径，防止测试连到生产记忆库。"""
    txt = cfg_path.read_text()
    txt = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{tmp / "no_qdrant"}"', txt)
    txt = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{tmp / "no_history.db"}"', txt)
    cfg_path.write_text(txt)


def test_loop_suppressed_refund_rolls_back_escape_valve():
    """F-RT-001: health=down 抑制发送时，evaluate 已记账的逃生阀决策必须被 refund
    （record_send_result failed + last_longing_break_at 回滚）。跑 run_loop.run() 驱动。"""
    from unittest.mock import patch
    import json as _json
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            engine = _escape_valve_engine(td)
            # health=down 状态文件（suppressed 判定的数据源，独立于 state 文件）
            health = Path(td) / "agent_health.json"
            health.write_text(_json.dumps({"state": "down", "fail_streak": 3,
                                           "last_fail_at": datetime.now().astimezone().isoformat()}))
            loop_cfg = {"health_state": str(health),
                        "bridge_url": f"http://127.0.0.1:{srv.server_port}"}
            engine.config["loop"] = loop_cfg
            # 消耗"进程内首次探针恒放行"的一次机会 → 第二次 _health_should_probe 读 down → 抑制
            assert engine._health_should_probe(loop_cfg) is True
            # 捕获 record_send_result 调用（退款发生的观测点）
            calls: list[tuple] = []
            orig = engine.record_send_result

            def spy(*args, **kw):
                calls.append(args)
                return orig(*args, **kw)
            engine.record_send_result = spy
            # run_loop 首个 run() 完成 suppressed 分支后进入休眠；以 KeyboardInterrupt 终止循环
            with patch.object(_loop_mod.time, "sleep", side_effect=KeyboardInterrupt):
                run_loop(engine, max_interval=900, compact=True)
            # 修复前红：suppressed 分支不调 refund → calls 为空 → 断言失败
            assert calls, "suppressed 分支应调用 record_send_result(msg_id, 'failed', 'suppressed'）退款"
            msg_id, status, err = calls[0]
            assert status == "failed", calls
            assert "suppressed" in (err or ""), calls
            # 逃生阀冷却被回滚（on-disk state 不再记录破防时刻）
            st = _json.loads(Path(td, "chiguo_state.json").read_text())
            assert st["cooldown"].get("last_longing_break_at") is None, \
                "suppressed 后逃生阀冷却应被退款回滚"
    finally:
        srv.shutdown()


def test_loop_spawn_injects_send_session_env():
    """F-A17-001: loop spawn 回退 agent-run 必须注入 AGENTRUN_SESSION（send_session_id）
    + AGENTRUN_ROTATE_SESSION=1，对齐 tick.sh —— 否则 send 会话落回复侧 chiguo-main 不轮换。
    修复前红：spawn env 缺注入 → fake script 读到空/默认 → 断言失败。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            env_log = Path(td) / "spawn_env.log"
            fake_runner = Path(td) / "fake-agent-env.mjs"
            fake_runner.write_text(
                "import { appendFileSync } from 'node:fs'\n"
                "const log=" + repr(str(env_log)) + "\n"
                "appendFileSync(log,'SESSION='+(process.env.AGENTRUN_SESSION||'')+"
                "'|ROTATE='+(process.env.AGENTRUN_ROTATE_SESSION||'')+'\\n')\n"
                "process.stdout.write(JSON.stringify({ ok: true, text: 'spawn 环境消息' }))\n"
            )
            old = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "error"   # RPC 失败 → 走 spawn 回退
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                out = engine._loop_send(_send_decision("m-env"), loop_cfg)
                assert out["generated"] is True and out["sent"] is True, out
                envs = env_log.read_text()
                assert "SESSION=chiguo-send" in envs, f"应注入 AGENTRUN_SESSION: {envs}"
                assert "ROTATE=1" in envs, f"应注入 AGENTRUN_ROTATE_SESSION=1: {envs}"
            finally:
                _restore_env(old, ah)
    finally:
        srv.shutdown()


# ═══════════════════════════════════════════════════════════
# R8 (F-A17-003): /send 超时不确定（timeout_uncertain）——不退款、不记 send_fail、
# 不重发（下轮自然再试）。对照：明确失败（ok:false 非 timeout）照旧退款 + send_fail。
# ═══════════════════════════════════════════════════════════

def test_loop_send_timeout_uncertain_no_refund_no_send_fail():
    """F-A17-003: /send 返回 timeout_uncertain → sent=false + 不记 send_fail +
    不调 record_send_result（不退款、不重发）。修复前红：当作明确失败退款+send_fail。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "owner@w"
                FakeBridge.send_mode = "timeout_uncertain"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                # 捕获 record_send_result 调用（退款发起观测点）
                calls: list[tuple] = []
                orig = engine.record_send_result

                def spy(*args, **kw):
                    calls.append(args)
                    return orig(*args, **kw)
                engine.record_send_result = spy
                out = engine._loop_send(_send_decision("m-uncertain"), loop_cfg)
                assert out["generated"] is True, out
                assert out.get("send_timeout_uncertain") is True, \
                    f"应标记 send_timeout_uncertain: {out}"
                assert out.get("sent") is False, out
                # 不退款：不得调用 record_send_result(failed)
                assert not calls, \
                    f"timeout_uncertain 不应触发退款 record_send_result: {calls}"
                # 不记 send_fail：agent_health 无 fail_streak 推进
                if Path(td, "agent_health.json").exists():
                    ah = json.loads(Path(td, "agent_health.json").read_text())
                    assert ah.get("fail_streak", 0) == 0, \
                        f"timeout_uncertain 不应推进 fail_streak: {ah}"
            finally:
                FakeBridge.send_mode = "ok"
    finally:
        srv.shutdown()


def test_loop_send_generate_fail_refunds():
    """F-RTS-001 (RF9): 生成失败分支（RPC 空回复 + spawn 失败，整链重试仍败）→
    除 record_health fail 外，还必须 record_send_result(msg_id, "failed")
    退款——否则 evaluate 已记账的 messages_without_reply/energy/Hawkes 残留，
    cron/loop 连续失败 → silent 禁发链。修复前红：生成失败分支不退款 → calls 为空。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-genfail.mjs"
            fake_runner.write_text(
                "process.stdout.write(JSON.stringify({ ok: false, error: '生成故障' }))")
            old_r = os.environ.get("AGENT_RUN_SCRIPT")
            ah = os.environ.get("AGENT_HEALTH_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "empty"   # RPC 恒空 → 走 spawn 回退 → 整链失败
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                # 捕获 record_send_result 调用（退款发起观测点）
                calls: list[tuple] = []
                orig = engine.record_send_result

                def spy(*args, **kw):
                    calls.append(args)
                    return orig(*args, **kw)
                engine.record_send_result = spy
                out = engine._loop_send(_send_decision("m-genfail"), loop_cfg)
                assert out["generated"] is False, out
                assert out.get("error"), out
                # record_health fail 记账不回归（fail_streak 推进、state=up 未达阈值）
                st = json.loads(Path(td, "agent_health.json").read_text())
                assert st.get("fail_streak") == 1, st
                assert st.get("state") == "up", st
                # 退款必须发生：生成失败分支调用 record_send_result(msg_id, "failed", ...)
                assert calls, \
                    "生成失败分支应调用 record_send_result(msg_id, 'failed', ...) 退款"
                msg_id, status, err = calls[0]
                assert msg_id == "m-genfail", calls
                assert status == "failed", calls
                assert "generate_failed" in (err or ""), calls
            finally:
                _restore_env(old_r, ah)
    finally:
        srv.shutdown()


def test_loop_send_clear_fail_still_refunds_send_fail():
    """F-A17-003 回归对照：明确失败（ok:false 非 timeout）→ 照旧退款 + send_fail。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            _use_health_script()
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "owner@w"
                FakeBridge.send_mode = "fail"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}",
                            "retry_delay_seconds": 0}
                out = engine._loop_send(_send_decision("m-clearfail"), loop_cfg)
                assert out["generated"] is True and out["sent"] is False, out
                assert not out.get("send_timeout_uncertain"), out
                ah = json.loads(Path(td, "agent_health.json").read_text())
                assert ah.get("fail_streak") == 1, \
                    f"明确失败应记 send_fail（fail_streak=1）: {ah}"
                assert "send failed" in ah.get("fail_reason", ""), ah
            finally:
                FakeBridge.send_mode = "ok"
    finally:
        srv.shutdown()
