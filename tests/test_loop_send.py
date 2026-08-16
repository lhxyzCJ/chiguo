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


class FakeBridge(BaseHTTPRequestHandler):
    """fake bridge：记录 /agent/prompt 与 /send 请求，响应可切换。"""

    requests = []  # (path, body)
    prompt_mode = "ok"   # ok | empty | error
    send_mode = "ok"     # ok=成功 | fail=/send 返回 ok:false（F-A6-2 发送失败域）

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
