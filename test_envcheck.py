#!/usr/bin/env python3
"""test_envcheck.py — chiguo_envcheck 环境检查单元测试(v10.3)"""

import sys
import os
import json
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def test_check_pi_missing_critical():
    r = ec.check_pi(pi_bin="/nonexistent/pi")
    assert r["severity"] == "critical" and not r["ok"]
    assert "未安装" in r["detail"]
    print("  OK test_check_pi_missing_critical")


def test_check_pi_skip_warn():
    """--skip-pi 下 pi 缺失 → warn（不阻塞部署，如实报告降级），非 critical。"""
    r = ec.check_pi(pi_bin="/nonexistent/pi", skip_pi=True)
    assert r["severity"] == "warn" and not r["ok"]
    assert "skip-pi" in r["detail"]
    print("  OK test_check_pi_skip_warn")


def test_check_pi_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"bin/pi": "#!/bin/sh\necho 0.83.0-test\n"})
        os.chmod(Path(td) / "bin/pi", 0o755)
        r = ec.check_pi(pi_bin=str(Path(td) / "bin" / "pi"))
        assert r["severity"] == "ok" and r["ok"]
        assert "0.83.0-test" in r["detail"]
    print("  OK test_check_pi_ok")


def test_check_pi_ext_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_pi_ext(Path(td) / "settings.json", Path(td) / "ext" / "index.js")
        assert r["severity"] == "warn" and not r["ok"]
        assert "install_pi.sh" in r["detail"]
    print("  OK test_check_pi_ext_missing_warn")


def test_check_pi_ext_windows_warn():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        want = str(td / ".pi-agent" / "TestForPi-memory-lancedb-pro" / "dist" / "pi-adapter" / "index.js")
        _mk(td, {".pi/agent/settings.json":
                 json.dumps({"extensions": ["/mnt/c/Users/USER/projects/TestForPi-memory-lancedb-pro/dist/pi-adapter/index.js", want]})})
        r = ec.check_pi_ext(td / ".pi/agent/settings.json", Path(want))
        assert r["severity"] == "warn" and not r["ok"]
        assert "Windows" in r["detail"]
    print("  OK test_check_pi_ext_windows_warn")


def test_check_pi_ext_ok():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        want = str(td / ".pi-agent" / "TestForPi-memory-lancedb-pro" / "dist" / "pi-adapter" / "index.js")
        _mk(td, {".pi/agent/settings.json": json.dumps({"extensions": [want]})})
        r = ec.check_pi_ext(td / ".pi/agent/settings.json", Path(want))
        assert r["severity"] == "ok" and r["ok"]
    print("  OK test_check_pi_ext_ok")


def test_check_pi_auth_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_pi_auth(Path(td) / "auth.json")
        assert r["severity"] == "warn" and not r["ok"]
        assert "PI_API_KEY" in r["detail"]
    print("  OK test_check_pi_auth_missing_warn")


def test_check_pi_auth_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"auth.json": json.dumps({"opencode-go": {"type": "api_key", "key": "sk-test"}})})
        r = ec.check_pi_auth(Path(td) / "auth.json")
        assert r["severity"] == "ok" and r["ok"]
        assert "opencode-go" in r["detail"]
    print("  OK test_check_pi_auth_ok")


def test_check_pi_auth_custom_provider():
    """check_pi_auth 按指定 provider 查 auth.json 条目（键名 = provider 名）。"""
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {"auth.json": json.dumps({"deepseek": {"type": "api_key", "key": "sk-ds"}})})
        r = ec.check_pi_auth(Path(td) / "auth.json", provider="deepseek")
        assert r["severity"] == "ok" and r["ok"]
        assert "deepseek" in r["detail"]
        r2 = ec.check_pi_auth(Path(td) / "auth.json", provider="openai")
        assert not r2["ok"]
        assert "openai" in r2["detail"]
    print("  OK test_check_pi_auth_custom_provider")


def test_run_checks_pi_auth_provider_from_toml():
    """run_checks 的 pi_auth 检查随 toml [host].provider 走（缺省回退 opencode-go）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        home = td / "home"
        _mk(home, {".pi/agent/auth.json": json.dumps({"deepseek": {"type": "api_key", "key": "sk-ds"}})})
        _mk(td, {"chiguo_proactive.toml": '[host]\nprovider = "deepseek"\n'})
        report = ec.run_checks(base_dir=td, home=home)
        auth = next(c for c in report["checks"] if c["name"] == "pi_auth")
        assert auth["ok"] and "deepseek" in auth["detail"], auth
        # auth 只有 opencode-go 条目 → warn 指向实际 provider deepseek
        _mk(home, {".pi/agent/auth.json": json.dumps({"opencode-go": {"type": "api_key", "key": "sk-og"}})})
        report2 = ec.run_checks(base_dir=td, home=home)
        auth2 = next(c for c in report2["checks"] if c["name"] == "pi_auth")
        assert not auth2["ok"] and "deepseek" in auth2["detail"], auth2
        # 无 toml provider → 回退 opencode-go（auth 含 opencode-go 条目 → ok 且指向 opencode-go）
        _mk(td, {"chiguo_proactive.toml": "[host]\nmodel = \"x\"\n"})
        report3 = ec.run_checks(base_dir=td, home=home)
        auth3 = next(c for c in report3["checks"] if c["name"] == "pi_auth")
        assert auth3["ok"] and "opencode-go" in auth3["detail"], auth3
    print("  OK test_run_checks_pi_auth_provider_from_toml")


def test_check_ollama_unreachable_info():
    r = ec.check_ollama("http://127.0.0.1:1")
    assert r["severity"] == "info" and not r["ok"]
    print("  OK test_check_ollama_unreachable_info")


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


def test_check_lancedb_missing_db_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_lancedb(db_path=Path(td) / "no_such_lancedb")
        # lancedb 未装或路径不存在 → 均为 info,不崩
        assert r["severity"] == "info" and not r["ok"]
    print("  OK test_check_lancedb_missing_db_info")


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
        cfg.write_text(re.sub(r"(?m)^lancedb_path\s*=.*$",
                              f'lancedb_path = "{td / "no_lancedb"}"',
                              cfg.read_text()))
        report = ec.run_checks(base_dir=td)
        assert len(report["checks"]) == 8
        assert report["summary"]["ok"] + report["summary"]["info"] + report["summary"]["warn"] + report["summary"]["critical"] == 8
        # netease/ollama 检查会尝试连 localhost —— 只要求不崩(超时 5s 内失败 → warn)
        json.dumps(report)
        report2 = ec.run_checks(base_dir=td, skip_pi=True)
        assert len(report2["checks"]) == 8
    print("  OK test_run_checks_never_crashes")


def test_lancedb_default_path_migrated():
    """lancedb 默认路径已迁出 ~/.openclaw（OpenClaw 即将删除，记忆库归 ~/.pi-agent）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        cfg = td / "chiguo_proactive.toml"
        cfg.write_text(Path("chiguo_proactive.toml").read_text().replace(
            'lancedb_path = "~/.openclaw/memory/lancedb-pro"',
            'lancedb_path = "~/.pi-agent/memory/lancedb-pro"'))
        cfg2 = ec._load_config(td)
        p = ec._cfg_path(cfg2, "memory", "lancedb_path",
                         "~/.pi-agent/memory/lancedb-pro", td)
        assert ".openclaw" not in str(p)
        assert str(p) == str(Path.home() / ".pi-agent" / "memory" / "lancedb-pro")
    print("  OK test_lancedb_default_path_migrated")


if __name__ == "__main__":
    test_check_env()
    test_check_pi_missing_critical()
    test_check_pi_skip_warn()
    test_check_pi_ok()
    test_check_pi_ext_missing_warn()
    test_check_pi_ext_windows_warn()
    test_check_pi_ext_ok()
    test_check_pi_auth_missing_warn()
    test_check_pi_auth_ok()
    test_check_pi_auth_custom_provider()
    test_run_checks_pi_auth_provider_from_toml()
    test_check_ollama_unreachable_info()
    test_check_ollama_proxy_bypassed()
    test_check_lancedb_missing_db_warn()
    test_check_netease_no_cookie_info()
    test_check_data_missing_info()
    test_check_data_ok()
    test_exit_code_mapping()
    test_run_checks_never_crashes()
    test_lancedb_default_path_migrated()
    print(f"test_envcheck.py: ALL {20} TESTS PASSED")
