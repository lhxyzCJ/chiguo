#!/usr/bin/env python3
"""agent_health.py — agent 假死状态机（跨进程共享状态 + transition 告警文案）。

用法:
  agent_health.py record --outcome fail|success [--reason <r>] [--config <path>] [--state <path>]

stdout JSON: {"state": up|down, "transition": none|down|up, "message": 告警/恢复文案, "fail_streak": N}
transition 只在 up→down 与 down→up 各输出一次（天然防重复告警）。
锁获取失败（5s 超时）→ 本次不写并 stderr 告警（宁丢一次记账，不无锁写共享 .tmp）。
"""

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
DEFAULT_THRESHOLD = 3
LOCK_TIMEOUT_S = 5.0
_LOCK_FDS = {}
_LOCK_DEPTH = {}


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
    if _LOCK_DEPTH.get(lock_path, 0) > 0:
        return False
    fd = _LOCK_FDS.get(lock_path)
    if fd is None:
        try:
            fd = open(lock_path, "a+")
        except OSError:
            return False
        try:
            deadline = time.monotonic() + LOCK_TIMEOUT_S
            while True:
                try:
                    fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        fd.close()
                        return False
                    time.sleep(0.1)
        except OSError:
            fd.close()
            return False
        _LOCK_FDS[lock_path] = fd
    _LOCK_DEPTH[lock_path] = 1
    return True


def _release(lock_path):
    if _LOCK_DEPTH.get(lock_path, 0) <= 0:
        return
    _LOCK_DEPTH.pop(lock_path, None)
    fd = _LOCK_FDS.pop(lock_path, None)
    if fd is not None:
        try:
            fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            fd.close()
        except OSError:
            pass


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

        if outcome == "fail":
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

        tmp = state_path.with_name(state_path.name + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
        os.replace(tmp, state_path)
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
    rec.add_argument("--outcome", choices=("fail", "success"), required=True)
    rec.add_argument("--reason", default=None)
    rec.add_argument("--config", default=str(_anchor() / "chiguo_proactive.toml"))
    rec.add_argument("--state", default=str(_anchor() / "agent_health.json"))
    args = p.parse_args()
    result = record(args.outcome, args.reason, Path(args.state), Path(args.config))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    cli()
