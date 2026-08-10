#!/usr/bin/env python3
"""test_envcheck.py — chiguo_envcheck 环境检查单元测试(v10.3)"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import chiguo_envcheck as ec


def _mk(tmp: Path, files: dict) -> Path:
    """在临时目录下创建文件树(files: {相对路径: 内容}),返回根目录。"""
    for rel, content in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return tmp


def test_check_env():
    r = ec.check_env()
    assert r["name"] == "env"
    assert r["severity"] in ("ok", "critical")
    print("  OK test_check_env")


def test_check_agent_missing_critical():
    r = ec.check_agent(agent_bin="/nonexistent/pi")
    assert r["severity"] == "critical" and not r["ok"]
    assert "未安装" in r["detail"]
    print("  OK test_check_agent_missing_critical")


def test_check_agent_skip_warn():
    """--skip-agent 下 agent 缺失 → warn（不阻塞部署，如实报告降级），非 critical。"""
    r = ec.check_agent(agent_bin="/nonexistent/pi", skip_agent=True)
    assert r["severity"] == "warn" and not r["ok"]
    assert "skip-agent" in r["detail"]
    print("  OK test_check_agent_skip_warn")


def test_check_agent_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"bin/pi": "#!/bin/sh\necho 0.83.0-test\n"})
        os.chmod(Path(td) / "bin/pi", 0o755)
        r = ec.check_agent(agent_bin=str(Path(td) / "bin" / "pi"))
        assert r["severity"] == "ok" and r["ok"]
        assert "0.83.0-test" in r["detail"]
    print("  OK test_check_agent_ok")


def test_check_agent_auth_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_agent_auth(Path(td) / "auth.json")
        assert r["severity"] == "warn" and not r["ok"]
        assert "AGENT_API_KEY" in r["detail"]
    print("  OK test_check_agent_auth_missing_warn")


def test_check_agent_auth_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"auth.json": json.dumps({"opencode-go": {"type": "api_key", "key": "sk-test"}})})
        r = ec.check_agent_auth(Path(td) / "auth.json")
        assert r["severity"] == "ok" and r["ok"]
        assert "opencode-go" in r["detail"]
    print("  OK test_check_agent_auth_ok")


def test_check_agent_auth_custom_provider():
    """check_agent_auth 按指定 provider 查 auth.json 条目（键名 = provider 名）。"""
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"auth.json": json.dumps({"deepseek": {"type": "api_key", "key": "sk-ds"}})})
        r = ec.check_agent_auth(Path(td) / "auth.json", provider="deepseek")
        assert r["severity"] == "ok" and r["ok"]
        assert "deepseek" in r["detail"]
        r2 = ec.check_agent_auth(Path(td) / "auth.json", provider="openai")
        assert not r2["ok"]
        assert "openai" in r2["detail"]
    print("  OK test_check_agent_auth_custom_provider")


def test_run_checks_agent_auth_provider_from_toml():
    """run_checks 的 agent_auth 检查随 toml [host].provider 走（缺省回退 opencode-go）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home = td / "home"
        _mk(home, {".pi/agent/auth.json": json.dumps({"deepseek": {"type": "api_key", "key": "sk-ds"}})})
        _mk(td, {"chiguo_proactive.toml": '[host]\nprovider = "deepseek"\n'})
        report = ec.run_checks(base_dir=td, home=home)
        auth = next(c for c in report["checks"] if c["name"] == "agent_auth")
        assert auth["ok"] and "deepseek" in auth["detail"], auth
        # auth 只有 opencode-go 条目 → warn 指向实际 provider deepseek
        _mk(home, {".pi/agent/auth.json": json.dumps({"opencode-go": {"type": "api_key", "key": "sk-og"}})})
        report2 = ec.run_checks(base_dir=td, home=home)
        auth2 = next(c for c in report2["checks"] if c["name"] == "agent_auth")
        assert not auth2["ok"] and "deepseek" in auth2["detail"], auth2
        # 无 toml provider → 回退 opencode-go（auth 含 opencode-go 条目 → ok 且指向 opencode-go）
        _mk(td, {"chiguo_proactive.toml": "[host]\nmodel = \"x\"\n"})
        report3 = ec.run_checks(base_dir=td, home=home)
        auth3 = next(c for c in report3["checks"] if c["name"] == "agent_auth")
        assert auth3["ok"] and "opencode-go" in auth3["detail"], auth3
    print("  OK test_run_checks_agent_auth_provider_from_toml")


def test_check_ollama_unreachable_warn():
    """Bug3: ollama 不可达 → warn(记忆 embedding 缺失影响部署判定),非 info。"""
    r = ec.check_ollama("http://127.0.0.1:1")
    assert r["severity"] == "warn" and not r["ok"]
    assert "不可达" in r["detail"]
    print("  OK test_check_ollama_unreachable_warn")


def test_check_ollama_missing_warn_exit_code():
    """Bug3: ollama 缺失(不可达/无模型)→ severity=warn 且退出码=1(此前 info 被漏计)。"""
    # 无模型分支: 起一个返回空模型列表的本地服务
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            r = ec.check_ollama(f"http://127.0.0.1:{port}")
            assert not r["ok"] and r["severity"] == "warn", f"无模型应 warn: {r}"
            assert "qwen3-embedding" in r["detail"], r["detail"]
        finally:
            srv.shutdown()
    # 不可达分支同样 warn
    r2 = ec.check_ollama("http://127.0.0.1:1")
    assert not r2["ok"] and r2["severity"] == "warn", f"不可达应 warn: {r2}"
    # warn 计入 summary → exit_code 1(修复前 info 不计,误判为 0)
    report = {"summary": {"ok": 5, "info": 1, "warn": 1, "critical": 0}}
    assert ec.exit_code(report) == 1
    print("  OK test_check_ollama_missing_warn_exit_code")


def test_run_checks_ollama_warn_sets_exit_code():
    """Bug3 集成: run_checks 中 ollama 缺失(warn)计入 summary → exit_code=1。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _mk(td, {"chiguo_proactive.toml": '[host]\nrunner = "agent"\n'})
        orig = ec.check_ollama
        ec.check_ollama = lambda *a, **k: {"name": "ollama", "ok": False,
                                           "severity": "warn", "detail": "mock 缺失"}
        try:
            report = ec.run_checks(base_dir=td, home=td / "home", skip_agent=True)
        finally:
            ec.check_ollama = orig
        oll = next(c for c in report["checks"] if c["name"] == "ollama")
        assert oll["severity"] == "warn", oll
        assert ec.exit_code(report) == 1, report["summary"]
    print("  OK test_run_checks_ollama_warn_sets_exit_code")


def test_check_ollama_proxy_bypassed():
    """ollama 检查(127.0.0.1)必须绕过系统代理：http_proxy 指向死端口也不误判不可达。"""
    import http.server
    import socketserver
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"models": [{"name": "qwen3-embedding:0.6b"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
        port = srv.server_address[1]
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        saved = {k: os.environ.get(k) for k in
                 ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy")}
        for k in saved:
            os.environ.pop(k, None)
        os.environ["http_proxy"] = "http://127.0.0.1:1"
        try:
            r = ec.check_ollama(f"http://127.0.0.1:{port}")
            assert r["severity"] == "ok", r
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            srv.shutdown()
    print("  OK test_check_ollama_proxy_bypassed")


def test_check_mem0_missing_dir_info():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_mem0(Path(td) / "no_such_qdrant", Path(td) / "no_history.db")
        # mem0ai 未装 / key 缺失 / 目录不存在 → 均为 info,不崩
        assert r["severity"] == "info" and not r["ok"]
    print("  OK test_check_mem0_missing_dir_info")


def test_check_mem0_ok_branch():
    """check_mem0 ok 分支：mem0ai 可导入 + key 存在 + qdrant 目录就绪 → severity=ok。"""
    import chiguo_envcheck as _ec
    with tempfile.TemporaryDirectory() as td:
        qdir = Path(td) / "qdrant"
        qdir.mkdir()
        orig = _ec._pi_api_key
        _ec._pi_api_key = lambda: "test-key"
        try:
            r = _ec.check_mem0(qdir, Path(td) / "no_history.db")
            assert r["severity"] == "ok" and r["ok"], f"ok 分支应返回 ok: {r}"
            # 历史库存在时同样 ok（两个 return 分支都覆盖）
            hist = Path(td) / "history.db"
            hist.write_text("x")
            r2 = _ec.check_mem0(qdir, hist)
            assert r2["severity"] == "ok" and r2["ok"]
        finally:
            _ec._pi_api_key = orig
    print("  OK test_check_mem0_ok_branch")


def test_check_netease_no_cookie_info():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_netease("http://127.0.0.1:1/", Path(td) / "netease_cookie.txt",
                             Path(td) / "netease_health.json")
        assert r["severity"] == "info" and not r["ok"]
        assert "login" in r["detail"]
    print("  OK test_check_netease_no_cookie_info")


def test_check_data_missing_info():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_data(Path(td) / "xskb.xlsx", Path(td) / "mem.json")
        assert r["severity"] == "info" and not r["ok"]
    print("  OK test_check_data_missing_info")


def test_check_data_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"xskb.xlsx": "x", "mem.json": "{}"})
        r = ec.check_data(Path(td) / "xskb.xlsx", Path(td) / "mem.json")
        assert r["severity"] == "ok" and r["ok"]
    print("  OK test_check_data_ok")


def test_exit_code_mapping():
    assert ec.exit_code({"summary": {"ok": 5, "warn": 0, "critical": 0}}) == 0
    assert ec.exit_code({"summary": {"ok": 4, "warn": 1, "critical": 0}}) == 1
    assert ec.exit_code({"summary": {"ok": 3, "warn": 1, "critical": 1}}) == 2
    print("  OK test_exit_code_mapping")


def test_run_checks_never_crashes():
    """run_checks 在任何环境下都不抛异常(注入临时 home,真实 toml 副本)。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = td / "chiguo_proactive.toml"
        cfg.write_text(Path("chiguo_proactive.toml").read_text())
        import re
        cfg.write_text(re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                              f'mem0_qdrant_path = "{td / "no_qdrant"}"',
                              cfg.read_text()))
        # #99: runner 值 pi→agent（契约），真实 toml 副本同步替换以保持 8 项检查路径
        cfg.write_text(re.sub(r"(?m)^runner\s*=.*$", 'runner = "agent"', cfg.read_text()))
        report = ec.run_checks(base_dir=td)
        assert len(report["checks"]) == 7
        assert report["summary"]["ok"] + report["summary"]["info"] + report["summary"]["warn"] + report["summary"]["critical"] == 7
        # netease/ollama 检查会尝试连 localhost —— 只要求不崩(超时 5s 内失败 → warn)
        json.dumps(report)
        report2 = ec.run_checks(base_dir=td, skip_agent=True)
        assert len(report2["checks"]) == 7
    print("  OK test_run_checks_never_crashes")


def test_mem0_default_path_anchored():
    """mem0 记忆库路径：相对路径锚定 config 所在目录（_cfg_path 不展开错位）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = td / "chiguo_proactive.toml"
        cfg.write_text(Path("chiguo_proactive.toml").read_text())
        cfg2 = ec._load_config(td)
        p = ec._cfg_path(cfg2, "memory", "mem0_qdrant_path", "data/mem0/qdrant", td)
        assert str(p) == str(td / "data" / "mem0" / "qdrant")
    print("  OK test_mem0_default_path_anchored")


def test_run_checks_custom_backend_skips_mem0():
    """自定义记忆后端类路径（含 .）→ envcheck 不直检 mem0，改报 memory_backend 提示。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        _mk(td, {"chiguo_proactive.toml": '[memory]\nbackend = "mymodule.MyBackend"\n'})
        report = ec.run_checks(base_dir=td, skip_agent=True)
        names = [c["name"] for c in report["checks"]]
        assert "mem0" not in names, names
        assert "memory_backend" in names, names
        mb = next(c for c in report["checks"] if c["name"] == "memory_backend")
        assert mb["ok"] and "mymodule.MyBackend" in mb["detail"], mb
        assert len(report["checks"]) == 7
    print("  OK test_run_checks_custom_backend_skips_mem0")


def test_sanitize_url_strips_credentials():
    """R24: _sanitize_url 剥离 userinfo 与 query/fragment(token/key 防泄漏)"""
    # query 带 token/key → 剥离 query(路径保留)
    s = ec._sanitize_url("http://localhost:11434/?token=SECRET")
    assert "token" not in s and "SECRET" not in s, s
    assert s == "http://localhost:11434/", s
    assert "SECRET" not in ec._sanitize_url("http://localhost:11434/v1?key=abc#frag")
    # userinfo(user:pass@) → 剥离
    s = ec._sanitize_url("http://user:pass@localhost:11434")
    assert s == "http://localhost:11434", s
    assert "pass" not in s
    # 干净 URL / 无法解析输入 → 原样保留
    assert ec._sanitize_url("http://localhost:11434") == "http://localhost:11434"
    assert ec._sanitize_url("http://host/path") == "http://host/path"
    print("  OK test_sanitize_url_strips_credentials")


def test_sanitize_url_bad_port_and_ipv6():
    """G8 自审: 非数字端口不崩 + IPv6 字面量重建保留方括号。"""
    # 非数字端口(如 OLLAMA_BASE 误配为 http://host:abc?token=) → 不抛异常、不泄漏 SECRET
    s = ec._sanitize_url("http://localhost:abc?token=SECRET")
    assert "SECRET" not in s and "abc" not in s, s
    assert s == "http://localhost", s
    # IPv6 字面量 → 重建含方括号(hostname 去括号 ::1,urlunsplit 需 [::1])
    s = ec._sanitize_url("http://[::1]:11434?token=x")
    assert s == "http://[::1]:11434", s
    print("  OK test_sanitize_url_bad_port_and_ipv6")


if __name__ == "__main__":
    test_check_env()
    test_check_agent_missing_critical()
    test_check_agent_skip_warn()
    test_check_agent_ok()
    test_check_agent_auth_missing_warn()
    test_check_agent_auth_ok()
    test_check_agent_auth_custom_provider()
    test_run_checks_agent_auth_provider_from_toml()
    test_check_ollama_unreachable_warn()
    test_check_ollama_missing_warn_exit_code()
    test_run_checks_ollama_warn_sets_exit_code()
    test_check_ollama_proxy_bypassed()
    test_check_mem0_missing_dir_info()
    test_check_mem0_ok_branch()
    test_check_netease_no_cookie_info()
    test_check_data_missing_info()
    test_check_data_ok()
    test_exit_code_mapping()
    test_run_checks_never_crashes()
    test_mem0_default_path_anchored()
    test_run_checks_custom_backend_skips_mem0()
    test_sanitize_url_strips_credentials()
    test_sanitize_url_bad_port_and_ipv6()
    print(f"test_envcheck.py: ALL {23} TESTS PASSED")
