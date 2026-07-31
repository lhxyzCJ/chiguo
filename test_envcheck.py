#!/usr/bin/env python3
"""test_envcheck.py — chiguo_envcheck 环境检查单元测试(v10.1)"""

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


def test_check_openclaw_missing_dir_critical():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_openclaw(personality_dir=Path(td) / "no_such_skill_dir")
        assert r["severity"] == "critical" and not r["ok"]
        assert "不存在" in r["detail"]
    print("  OK test_check_openclaw_missing_dir_critical")


def test_check_openclaw_skill_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {".openclaw/workspace/skills/chiguo/empty.txt": "x"})
        r = ec.check_openclaw(personality_dir=Path(td) / ".openclaw/workspace/skills/chiguo")
        assert r["severity"] == "warn" and not r["ok"]
        assert "SUN2.md" in r["detail"] and "SKILL.md" in r["detail"]
    print("  OK test_check_openclaw_skill_missing_warn")


def test_check_openclaw_ok():
    with tempfile.TemporaryDirectory() as td:
        _mk(Path(td), {".openclaw/workspace/skills/chiguo/SUN2.md": "s",
                       ".openclaw/workspace/skills/chiguo/SKILL.md": "k"})
        r = ec.check_openclaw(personality_dir=Path(td) / ".openclaw/workspace/skills/chiguo")
        assert r["severity"] == "ok" and r["ok"]
    print("  OK test_check_openclaw_ok")


def test_check_lancedb_missing_db_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_lancedb(db_path=Path(td) / "no_such_lancedb")
        # lancedb 未装或路径不存在 → 均为 warn,不崩
        assert r["severity"] == "warn" and not r["ok"]
    print("  OK test_check_lancedb_missing_db_warn")


def test_check_netease_no_cookie_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_netease("http://127.0.0.1:1/", Path(td) / "netease_cookie.txt",
                             Path(td) / "netease_health.json")
        assert r["severity"] == "warn" and not r["ok"]
        assert "login" in r["detail"]
    print("  OK test_check_netease_no_cookie_warn")


def test_check_data_missing_warn():
    with tempfile.TemporaryDirectory() as td:
        r = ec.check_data(Path(td) / "xskb.xlsx", Path(td) / "mem.json")
        assert r["severity"] == "warn" and not r["ok"]
    print("  OK test_check_data_missing_warn")


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
        assert len(report["checks"]) == 5
        assert report["summary"]["ok"] + report["summary"]["warn"] + report["summary"]["critical"] == 5
        # netease 检查会尝试连 localhost:3000 —— 只要求不崩(超时 5s 内失败 → warn)
        json.dumps(report)
    print("  OK test_run_checks_never_crashes")


if __name__ == "__main__":
    test_check_env()
    test_check_openclaw_missing_dir_critical()
    test_check_openclaw_skill_missing_warn()
    test_check_openclaw_ok()
    test_check_lancedb_missing_db_warn()
    test_check_netease_no_cookie_warn()
    test_check_data_missing_warn()
    test_check_data_ok()
    test_exit_code_mapping()
    test_run_checks_never_crashes()
    print(f"test_envcheck.py: ALL {10} TESTS PASSED")
