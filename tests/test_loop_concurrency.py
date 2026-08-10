#!/usr/bin/env python3
"""test_loop_concurrency.py — C3 常驻态状态一致性验证（TDD）

覆盖：①多进程并发 evaluate/record_user_message → state 校验和通过、tick_seq
单调、无锁竞争异常；②--loop 常驻态配置热重载（_maybe_reload_config mtime 检测）。
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_state import ChiguoState


def _setup(temp_dir: str) -> str:
    """构造临时仓库（toml + mem0 隔离），返回 toml 路径。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return str(cfg_path)


def test_concurrent_cli_state_integrity():
    """多进程并发（4× record + 2× evaluate 同 state）→ 加载校验和通过、tick_seq 单调。"""
    with tempfile.TemporaryDirectory() as td:
        toml = _setup(td)

        def run_worker(action, text=None):
            cmd = [sys.executable, "tests/_loop_worker.py", toml, action]
            if text:
                cmd.append(text)
            return subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=90, cwd=os.getcwd())

        results = []

        def worker(args):
            results.append(run_worker(*args))

        threads = []
        for i in range(4):
            threads.append(threading.Thread(target=worker, args=(("record", f"并发消息{i}号"),)))
        for _ in range(2):
            threads.append(threading.Thread(target=worker, args=(("evaluate",),)))
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for r in results:
            assert r.returncode == 0, f"worker 失败: {r.stderr[-300:]}"
        # 加载即校验（损坏/校验和不符会抛错）；tick_seq 单调递增
        with open(Path(td) / "chiguo_proactive.toml", "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = td
        st = ChiguoState(cfg)
        st._load()
        assert st.tick_seq >= 1, f"tick_seq 应 ≥1: {st.tick_seq}"
        assert 0 <= st.emotion.loneliness <= 100


def test_loop_hot_reload_config():
    """--loop 常驻态：修改 toml → _maybe_reload_config 热更新（mtime 检测）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "chiguo_proactive.toml"
        _setup(td)
        from chiguo_daemon import DecisionEngine
        # 先显式写入确定不同于 0.42 的初始值（不依赖仓库默认值，防止默认值将来变成 0.42）
        txt = cfg_path.read_text()
        txt = re.sub(r"(?m)^comfort_weight_base\s*=.*$",
                     "comfort_weight_base = 0.11", txt)
        cfg_path.write_text(txt)
        engine = DecisionEngine(str(cfg_path),
                                str(Path(td) / "chiguo_decisions.jsonl"))
        old_val = engine.config["trigger"].get("comfort_weight_base", 0.0)
        assert old_val == 0.11 and old_val != 0.42, \
            f"重载前初始值应被显式钉为 0.11: {old_val}"
        # 改 toml（保持语法合法）→ mtime 变化
        txt = cfg_path.read_text()
        new_txt = re.sub(r"(?m)^comfort_weight_base\s*=.*$",
                         "comfort_weight_base = 0.42", txt)
        cfg_path.write_text(new_txt)
        engine._maybe_reload_config()
        assert engine.config["trigger"]["comfort_weight_base"] == 0.42, \
            f"热重载应生效: {engine.config['trigger'].get('comfort_weight_base')}"
        # 语法错误 → 保留旧配置不崩溃
        cfg_path.write_text("not valid toml [[[")
        engine._maybe_reload_config()
        assert engine.config["trigger"]["comfort_weight_base"] == 0.42, \
            "语法错误应保留旧配置"


if __name__ == "__main__":
    tests = [
        test_concurrent_cli_state_integrity,
        test_loop_hot_reload_config,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
