#!/usr/bin/env python3
"""agent_health.py — agent 假死状态机（跨进程共享状态 + transition 告警文案）。

用法:
  agent_health.py record --outcome fail|send_fail|success [--reason <r>] [--config <path>] [--state <path>]

stdout JSON: {"state": up|down, "transition": none|down|up, "message": 告警/恢复文案, "fail_streak": N}
transition 只在 up→down 与 down→up 各输出一次（天然防重复告警）。
锁获取失败（5s 超时）→ 本次不写并 stderr 告警（宁丢一次记账，不无锁写共享 .tmp）。
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# agent_health.py 以独立脚本执行（scripts/ 在 sys.path[0]，仓库根不在）→ 显式把
# 仓库根加入 sys.path 以导入共享模块 chiguo_locks/chiguo_time/chiguo_atomic。
# 仓库根候选：优先 __file__ 的父父目录（产线/直跑即仓库根）；测试把本脚本拷贝到
# tmp 目录执行时该路径退化为 tmp，故追加 CWD 兜底（测试/桥进程的 CWD = 仓库根）。
_REPO_CANDIDATES = [
    str(Path(__file__).resolve().parent.parent),
    str(Path.cwd()),
]
for _c in _REPO_CANDIDATES:
    if _c and _c not in sys.path:
        sys.path.insert(0, _c)

from chiguo_time import CST
import chiguo_locks as locks
from chiguo_atomic import atomic_write

DEFAULT_THRESHOLD = 3


def _anchor():
    return Path(__file__).resolve().parent.parent


def _read_threshold(config_path):
    try:
        import tomllib
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
        t = cfg.get("health", {}).get("fail_threshold", DEFAULT_THRESHOLD)
        if isinstance(t, bool) or not isinstance(t, int):
            return DEFAULT_THRESHOLD
        return t if t >= 1 else DEFAULT_THRESHOLD
    except Exception:
        return DEFAULT_THRESHOLD


def _read_state(state_path):
    if not state_path.exists():
        return {"state": "up", "fail_streak": 0}
    try:
        data = json.loads(state_path.read_text())
        if not isinstance(data, dict):
            raise ValueError("state not a dict")
        return data
    except Exception:
        return {"state": "up", "fail_streak": 0}


def _acquire(lock_path):
    return locks.acquire(lock_path)


def _release(lock_path):
    locks.release(lock_path)


def _build_message(state, streak, reason):
    if state == "down":
        r = reason or "未知"
        return (f"⚠️ 后端异常：pi-agent 连续 {streak} 次调用失败（原因：{r}）。"
                "回复和主动消息都会受影响，我还在线但脑子不转了～恢复后告诉你")
    return "✅ 后端已恢复，我活过来了！"


def record(outcome, reason, state_path, config_path):
    lock_path = str(state_path) + ".lock"
    acquired = _acquire(lock_path)
    if not acquired:
        print(f"[agent_health] 锁获取失败，本次记账跳过: {state_path}", file=sys.stderr)
        return {"state": "up", "transition": "none", "message": "", "fail_streak": 0}
    try:
        st = _read_state(state_path)
        now = datetime.now(CST).isoformat()
        transition = "none"
        message = ""

        if outcome in ("fail", "send_fail"):
            # R7 (F-A17-002): fail_streak 有界 —— down 态不再无条件 +1（否则无界增长）。
            # down 后 fail_streak 封顶在阈值；down→up 仅由 success 触发（下方 else 分支）。
            if st.get("state") != "down":
                st["fail_streak"] = st.get("fail_streak", 0) + 1
            if st.get("fail_reason") is None:
                st["fail_reason"] = reason
            st["last_fail_at"] = now
            if st.get("state") != "down" and st["fail_streak"] >= _read_threshold(config_path):
                st["state"] = "down"
                st["changed_at"] = now
                transition = "down"
                message = _build_message("down", st["fail_streak"], st.get("fail_reason"))
        else:
            st["last_success_at"] = now
            if st.get("state") == "down":
                st["state"] = "up"
                st["changed_at"] = now
                st["fail_streak"] = 0
                st["fail_reason"] = None
                transition = "up"
                message = _build_message("up", 0, None)
            else:
                st["fail_streak"] = 0
                st["fail_reason"] = None

        # Q23: 原子写收敛至共享 chiguo_atomic.atomic_write（0600 一步到位）。
        atomic_write(state_path, json.dumps(st, ensure_ascii=False, indent=2),
                     mode=0o600)
    finally:
        if acquired:
            _release(lock_path)

    return {
        "state": st["state"],
        "transition": transition,
        "message": message,
        "fail_streak": st["fail_streak"],
    }


def cli():
    p = argparse.ArgumentParser(description="agent 假死状态机")
    sub = p.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record")
    rec.add_argument("--outcome", choices=("fail", "send_fail", "success"), required=True)
    rec.add_argument("--reason", default=None)
    rec.add_argument("--config", default=str(_anchor() / "chiguo_proactive.toml"))
    rec.add_argument("--state", default=str(_anchor() / "agent_health.json"))
    args = p.parse_args()
    # F-A6-2: send_fail 缺省 reason 显式区分发送失败（bridge /send 故障），
    # 便于告警文案诊断（默认桥发送失败，调用方可不传 --reason）。
    reason = args.reason
    if args.outcome == "send_fail" and not reason:
        reason = "bridge send failed"
    result = record(args.outcome, reason, Path(args.state), Path(args.config))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    cli()
