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
        # PR-4 起 comfort_weight_base 归档至 [experimental] trigger__comfort_weight_base，
        # 写入后经 _merge_experimental 合并回 trigger，主段视为 0.11
        txt = cfg_path.read_text()
        # 归档形态：trigger__comfort_weight_base
        if "trigger__comfort_weight_base" in txt:
            txt = re.sub(r"(?m)^trigger__comfort_weight_base\s*=.*$",
                         "trigger__comfort_weight_base = 0.11", txt)
        else:
            txt = re.sub(r"(?m)^comfort_weight_base\s*=.*$",
                         "comfort_weight_base = 0.11", txt)
        cfg_path.write_text(txt)
        engine = DecisionEngine(str(cfg_path),
                                str(Path(td) / "chiguo_decisions.jsonl"))
        old_val = engine.config["trigger"].get("comfort_weight_base", 0.0)
        assert old_val == 0.11 and old_val != 0.42, \
            f"重载前初始值应被显式钉为 0.11: {old_val}"
        txt = cfg_path.read_text()
        if "trigger__comfort_weight_base" in txt:
            new_txt = re.sub(r"(?m)^trigger__comfort_weight_base\s*=.*$",
                             "trigger__comfort_weight_base = 0.42", txt)
        else:
            new_txt = re.sub(r"(?m)^comfort_weight_base\s*=.*$",
                             "comfort_weight_base = 0.42", txt)
        cfg_path.write_text(new_txt)
        engine._maybe_reload_config()
        assert engine.config["trigger"]["comfort_weight_base"] == 0.42, \
            f"热重载应生效: {engine.config['trigger'].get('comfort_weight_base')}"
        cfg_path.write_text("not valid toml [[[")
        engine._maybe_reload_config()
        assert engine.config["trigger"]["comfort_weight_base"] == 0.42, \
            "语法错误应保留旧配置"
def test_loop_hot_reload_rebuild_set():
    """Q19: 热重载重建集合——改 personality / cooldown 静默窗口起始 / holiday_parser
    相关配置 → _maybe_reload_config 后重建生效。"""
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "chiguo_proactive.toml"
        _setup(td)
        # 钉住初始值：personality 傲娇基线 + cooldown 静默窗口（不依赖仓库默认）
        txt = cfg_path.read_text()
        txt = re.sub(r"(?m)^tsundere_intensity\s*=.*$",
                     "tsundere_intensity = 31.0", txt, count=1)
        txt = re.sub(r"(?m)^quiet_start\s*=\s*(\d+)", "quiet_start = 21", txt)
        txt = re.sub(r"(?m)^quiet_end\s*=\s*(\d+)", "quiet_end = 23", txt)
        cfg_path.write_text(txt)

        from chiguo_daemon import DecisionEngine
        engine = DecisionEngine(str(cfg_path),
                                str(Path(td) / "chiguo_decisions.jsonl"))
        # 变更前基线断言（确定性，非默认值）
        assert engine.state._personality_initial_baseline["tsundere_intensity"] == 31.0, \
            f"personality 初始基线应钉为 31.0: {engine.state._personality_initial_baseline}"
        assert engine.state.cooldown.quiet_window() == (21, 23), \
            f"cooldown 静默窗口应钉为 (21,23): {engine.state.cooldown.quiet_window()}"
        old_hp = engine.state.holiday_parser

        # 修改 config：personality 傲娇 / cooldown 静默窗口
        txt = cfg_path.read_text()
        txt = re.sub(r"(?m)^tsundere_intensity\s*=.*$",
                     "tsundere_intensity = 79.0", txt, count=1)
        txt = re.sub(r"(?m)^quiet_start\s*=\s*(\d+)", "quiet_start = 1", txt)
        txt = re.sub(r"(?m)^quiet_end\s*=\s*(\d+)", "quiet_end = 3", txt)
        cfg_path.write_text(txt)

        engine._maybe_reload_config()

        # ① 改 personality 配置 → 初始基线重建生效
        assert engine.config["personality"]["tsundere_intensity"] == 79.0
        assert engine.state._personality_initial_baseline["tsundere_intensity"] == 79.0, \
            f"改 personality → _personality_initial_baseline 应重建: " \
            f"{engine.state._personality_initial_baseline['tsundere_intensity']}"
        # ② 改 cooldown 静默窗口起始 → quiet_window() 重建生效
        assert engine.state.cooldown.quiet_window() == (1, 3), \
            f"改 cooldown 静默窗口起始 → quiet_window() 应重建: " \
            f"{engine.state.cooldown.quiet_window()}"
        # ③ holiday_parser 随热重载重建（重读 holidays.json，新实例）
        assert engine.state.holiday_parser is not old_hp, \
            "holiday_parser 应随热重载重建（新实例）"
