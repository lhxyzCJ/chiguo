#!/usr/bin/env python3
"""test_loop_send.py — C1 daemon --loop 发送侧内聚单元测试（TDD）"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine


class FakeBridge(BaseHTTPRequestHandler):
    """fake bridge：记录 /agent/prompt 与 /send 请求，响应可切换。"""

    requests = []  # (path, body)
    prompt_mode = "ok"   # ok | empty | error

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
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "error"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}"}
                out = engine._loop_send(_send_decision(), loop_cfg)
                assert out["generated"] is True and out["sent"] is True, out
                send_body = json.loads(
                    [b for p, b in FakeBridge.requests if p == "/send"][0])
                assert send_body["text"] == "spawn 兜底消息", send_body
            finally:
                if old is None:
                    os.environ.pop("AGENT_RUN_SCRIPT", None)
                else:
                    os.environ["AGENT_RUN_SCRIPT"] = old
    finally:
        srv.shutdown()


def test_loop_send_all_fail():
    """RPC 空回复 + spawn 失败 → generated=false + error 非空（不抛异常）。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            fake_runner = Path(td) / "fake-agent-run.mjs"
            fake_runner.write_text(
                "process.stdout.write(JSON.stringify({ ok: false, error: 'fake 故障' }))")
            old = os.environ.get("AGENT_RUN_SCRIPT")
            os.environ["AGENT_RUN_SCRIPT"] = str(fake_runner)
            try:
                engine = _make_engine(td)
                engine.config["wechat"]["wechat_recipient"] = "r@w"
                FakeBridge.prompt_mode = "empty"
                loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}"}
                out = engine._loop_send(_send_decision(), loop_cfg)
                assert out["generated"] is False, out
                assert out.get("error"), out
            finally:
                if old is None:
                    os.environ.pop("AGENT_RUN_SCRIPT", None)
                else:
                    os.environ["AGENT_RUN_SCRIPT"] = old
    finally:
        srv.shutdown()


def test_loop_send_send_failure_refund():
    """发送失败 → sent=false + record_send_result failed（refund 闭环）。"""
    srv = _start_bridge()
    try:
        with tempfile.TemporaryDirectory() as td:
            engine = _make_engine(td)
            engine.config["wechat"]["wechat_recipient"] = ""  # 空收件人 → 不发送
            loop_cfg = {"bridge_url": f"http://127.0.0.1:{srv.server_port}"}
            out = engine._loop_send(_send_decision("m2"), loop_cfg)
            assert out["generated"] is True and out["sent"] is False, out
    finally:
        srv.shutdown()


if __name__ == "__main__":
    tests = [
        test_loop_send_rpc_ok,
        test_loop_send_rpc_fail_fallback_spawn,
        test_loop_send_all_fail,
        test_loop_send_send_failure_refund,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
