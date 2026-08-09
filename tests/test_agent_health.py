#!/usr/bin/env python3
# test_agent_health.py — agent_health.py 状态机独立 runner 测试（TDD: 先红后绿）
# 用法: uv run python test_agent_health.py（退出码 0=全过，1=有失败）
# 隔离: 全部用 temp dir 的 --state/--config，绝不碰真实 agent_health.json。

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGENT_HEALTH = ROOT / "scripts" / "agent_health.py"

passed = 0
failed = 0


def t(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  ok - {name}")
    except Exception as e:
        failed += 1
        print(f"  FAIL - {name}: {e}")


def run(outcome, state, config, reason=None):
    """调用 agent_health.py record，返回解析后的 stdout JSON。"""
    cmd = [sys.executable, str(AGENT_HEALTH), "record", "--outcome", outcome,
           "--state", str(state), "--config", str(config)]
    if reason:
        cmd += ["--reason", reason]
    p = subprocess.run(cmd, capture_output=True, text=True)
    assert p.returncode == 0, f"exit={p.returncode} stderr={p.stderr!r}"
    return json.loads(p.stdout)


def no_tmp_leftover(state_path):
    for f in state_path.parent.iterdir():
        if f.name.endswith(".tmp"):
            raise AssertionError(f"残留 .tmp 文件: {f.name}")


# ── 状态机全矩阵 ────────────────────────────────────────────────

def test_matrix():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state = td / "agent_health.json"
        cfg = td / "health.toml"
        cfg.write_text("[health]\nfail_threshold = 3\n")

        # 未达阈值：保持 up，无 transition
        r1 = run("fail", state, cfg, "网络抖动")
        assert r1["state"] == "up", r1
        assert r1["transition"] == "none", r1
        assert r1["fail_streak"] == 1, r1
        r2 = run("fail", state, cfg, "网络抖动2")
        assert r2["state"] == "up", r2
        assert r2["transition"] == "none", r2
        assert r2["fail_streak"] == 2, r2

        # 第 3 次失败：越过阈值 → down + transition=down + 告警文案含次数
        r3 = run("fail", state, cfg, "API key 失效")
        assert r3["state"] == "down", r3
        assert r3["transition"] == "down", r3
        assert "3" in r3["message"], r3
        assert r3["fail_streak"] == 3, r3

        # 已 down 再失败：不重复告警
        r4 = run("fail", state, cfg, "还是不行")
        assert r4["state"] == "down", r4
        assert r4["transition"] == "none", r4

        # 恢复：首次 success → up + transition=up + 恢复文案
        r5 = run("success", state, cfg)
        assert r5["state"] == "up", r5
        assert r5["transition"] == "up", r5
        assert "恢复" in r5["message"] or "恢复" in str(r5), r5

        # up 后 success：无 transition
        r6 = run("success", state, cfg)
        assert r6["state"] == "up", r6
        assert r6["transition"] == "none", r6

        no_tmp_leftover(state)


def test_reason_preserved_from_first_failure():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state = td / "agent_health.json"
        cfg = td / "health.toml"
        cfg.write_text("[health]\nfail_threshold = 3\n")
        run("fail", state, cfg, "原因A")
        r = run("fail", state, cfg, "原因B")
        r3 = run("fail", state, cfg, "原因C")
        assert r3["transition"] == "down", r3
        # 告警保留本串首次失败原因，便于诊断根因
        assert "原因A" in r3["message"], r3
        assert "原因B" not in r3["message"], r3


def test_threshold_from_toml():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state = td / "agent_health.json"
        cfg = td / "health.toml"
        cfg.write_text("[health]\nfail_threshold = 2\n")
        r1 = run("fail", state, cfg, "x")
        assert r1["state"] == "up", r1
        r2 = run("fail", state, cfg, "x")
        assert r2["state"] == "down", r2
        assert r2["transition"] == "down", r2
        assert r2["fail_streak"] == 2, r2


def test_threshold_fallback_without_toml():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state = td / "agent_health.json"
        missing_cfg = td / "does-not-exist.toml"
        r2 = run("fail", state, missing_cfg, "x")
        r3 = run("fail", state, missing_cfg, "x")
        assert r3["state"] == "up", r3
        r4 = run("fail", state, missing_cfg, "x")
        assert r4["state"] == "down", r4
        assert r4["transition"] == "down", r4


def test_threshold_invalid_values_fallback():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        for bad in (0, -1, 1.5, "abc"):
            state = td / f"agent_health_{str(bad).replace('.', '_').replace('-', 'm')}.json"
            cfg = td / f"health_{str(bad).replace('.', '_').replace('-', 'm')}.toml"
            cfg.write_text(f"[health]\nfail_threshold = {bad}\n")
            r2 = run("fail", state, cfg, "x")
            r3 = run("fail", state, cfg, "x")
            assert r3["state"] == "up", f"threshold={bad} 应回退 3: {r3}"
            r4 = run("fail", state, cfg, "x")
            assert r4["state"] == "down", f"threshold={bad} 应在第 3 次失败触发: {r4}"


def test_reason_recaptured_after_recovery():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state = td / "agent_health.json"
        cfg = td / "health.toml"
        cfg.write_text("[health]\nfail_threshold = 2\n")
        run("fail", state, cfg, "第一波A")
        r = run("fail", state, cfg, "第一波B")
        assert r["transition"] == "down", r
        assert "第一波A" in r["message"], r
        run("success", state, cfg)
        r2 = run("fail", state, cfg, "第二波X")
        assert r2["transition"] == "none", r2
        r3 = run("fail", state, cfg, "第二波Y")
        assert r3["transition"] == "down", r3
        assert "第二波X" in r3["message"], r3
        assert "第一波A" not in r3["message"], r3


def test_state_file_integrity():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        state = td / "agent_health.json"
        cfg = td / "health.toml"
        cfg.write_text("[health]\nfail_threshold = 3\n")
        for i in range(5):
            run("fail", state, cfg, f"e{i}")
        run("success", state, cfg)
        data = json.loads(state.read_text())
        assert data["state"] == "up", data
        assert data["fail_streak"] == 0, data
        no_tmp_leftover(state)


def test_help_and_bad_args():
    p = subprocess.run([sys.executable, str(AGENT_HEALTH), "--help"],
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    p2 = subprocess.run([sys.executable, str(AGENT_HEALTH), "record", "--outcome", "bogus"],
                        capture_output=True, text=True)
    assert p2.returncode != 0, "非法 outcome 应失败"


t("状态机全矩阵: 未达阈值→up/无transition; 越阈→down+告警; 去重; 恢复; up后无transition; 无.tmp残留", test_matrix)
t("告警保留本串首次失败原因（不被后续失败覆盖）", test_reason_preserved_from_first_failure)
t("阈值从 toml [health].fail_threshold 读取（=2 时第 2 次失败即 down）", test_threshold_from_toml)
t("toml 缺失 → 回退阈值 3", test_threshold_fallback_without_toml)
t("无效阈值（0/负数/小数/非数字）→ 回退 3", test_threshold_invalid_values_fallback)
t("恢复后新一轮失败重新捕获原因（不残留上一轮）", test_reason_recaptured_after_recovery)
t("状态文件完整性与恢复后 streak 清零", test_state_file_integrity)
t("--help 可用；非法 outcome 非零退出", test_help_and_bad_args)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
