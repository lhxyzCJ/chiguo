#!/usr/bin/env python3
# ============================================================
# chiguo_watchdog.py — 迟菓主动消息 独立看门狗
#
# 零依赖，纯 stdlib。可被 cron/systemd timer 独立调用。
# 不依赖 daemon 进程存活，直接读状态文件 + 日志 做判断。
#
# 用法：
#   python3 chiguo_watchdog.py              # 完整检查，输出 JSON
#   python3 chiguo_watchdog.py --quiet       # 仅异常时输出（退出码驱动）
#   python3 chiguo_watchdog.py --notify      # 触发通知（stderr 写告警摘要）
#
# 退出码：0=正常, 1=警告(warn), 2=严重(critical)
# ============================================================

import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
PROJECT_DIR = Path(__file__).resolve().parent


def now():
    return datetime.now(CST)


def check_daemon_tick(state_path: Path) -> dict:
    """检查 daemon 最近是否运行。"""
    result = {"ok": True, "detail": ""}
    if not state_path.exists():
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = "chiguo_state.json missing — daemon never run?"
        return result

    try:
        data = json.loads(state_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = f"chiguo_state.json corrupted: {e}"
        return result

    last_tick = data.get("last_tick")
    if not last_tick:
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = "no last_tick in state file"
        return result

    try:
        lt = datetime.fromisoformat(last_tick)
        hours_ago = (now() - lt).total_seconds() / 3600
    except (ValueError, TypeError):
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = f"last_tick parse error: {last_tick}"
        return result

    result["hours_ago"] = round(hours_ago, 1)
    result["tick_seq"] = data.get("tick_seq")  # v5: forward progress indicator

    # ── v5: tick_seq 前向比对 ──
    ws_path = PROJECT_DIR / "chiguo_watchdog_state.json"
    prev = {}
    if ws_path.exists():
        try:
            prev = json.loads(ws_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    prev_seq = prev.get("tick_seq")
    curr_seq = result["tick_seq"]
    # tick_seq 回退（< prev_seq）→ state 文件被删/重建后重启（tick_seq 归 1），
    # 不是停滞：重置 stall_since、不告警。相等且长时间不增才告警。
    tick_restarted = (
        prev_seq is not None and curr_seq is not None and curr_seq < prev_seq
    )
    stalled = (
        not tick_restarted
        and prev_seq is not None
        and curr_seq is not None
        and curr_seq <= prev_seq
    )
    stall_since = prev.get("stall_since")
    if stalled and not stall_since:
        stall_since = now().isoformat()
    elif not stalled:
        stall_since = None

    stall_detected = False
    if stalled and stall_since:
        try:
            hours_stale = (now() - datetime.fromisoformat(stall_since)).total_seconds() / 3600
        except (ValueError, TypeError):
            hours_stale = 0
        if hours_stale > 3:
            stall_detected = True
            result["tick_stalled"] = True
            result["tick_stall_detail"] = (
                f"tick_seq stuck at {result['tick_seq']} since {stall_since} ({hours_stale:.1f}h)"
            )
    elif tick_restarted:
        result["tick_restarted"] = True
    # 持久化本次（.tmp + os.replace 原子写，防半写损坏）
    try:
        tmp = ws_path.with_name(ws_path.name + ".tmp")
        tmp.write_text(json.dumps({
            "tick_seq": result["tick_seq"],
            "checked_at": now().isoformat(),
            "stall_since": stall_since,
        }, ensure_ascii=False))
        os.replace(tmp, ws_path)
    except OSError:
        pass

    if hours_ago > 12:
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = f"daemon last tick {hours_ago:.1f}h ago (>12h)"
        if stall_detected:
            result["detail"] += " | tick_seq stalled"
    elif hours_ago > 6:
        result["ok"] = False
        result["severity"] = "warn"
        result["detail"] = f"daemon last tick {hours_ago:.1f}h ago (>6h)"
        if stall_detected:
            result["detail"] += " | tick_seq stalled"
    elif stall_detected:
        result["ok"] = False
        result["severity"] = "warn"
        result["detail"] = f"daemon OK (last tick {hours_ago:.1f}h ago) | tick_seq stalled"
    elif tick_restarted:
        result["severity"] = "ok"
        result["detail"] = f"daemon OK (last tick {hours_ago:.1f}h ago) | tick_seq restarted"
    else:
        result["severity"] = "ok"
        result["detail"] = f"daemon OK (last tick {hours_ago:.1f}h ago)"
    return result


def check_log_freshness(log_path: Path) -> dict:
    """检查决策日志是否有最近写入。"""
    result = {"ok": True, "detail": ""}
    if not log_path.exists():
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = "chiguo_decisions.jsonl missing"
        return result

    try:
        mtime = datetime.fromtimestamp(log_path.stat().st_mtime, tz=CST)
        hours_ago = (now() - mtime).total_seconds() / 3600
    except OSError as e:
        result["ok"] = False
        result["severity"] = "critical"
        result["detail"] = f"cannot stat decisions log: {e}"
        return result

    result["hours_ago"] = round(hours_ago, 1)
    if hours_ago > 24:
        result["ok"] = False
        result["severity"] = "warn"
        result["detail"] = f"decisions log last modified {hours_ago:.1f}h ago (>24h)"
    elif hours_ago > 12:
        result["detail"] = f"decisions log last modified {hours_ago:.1f}h ago (watching)"
        result["severity"] = "info"
    else:
        result["detail"] = f"decisions log OK (modified {hours_ago:.1f}h ago)"
    return result


def check_disk_space(path: Path, warn_mb: int = 500, critical_mb: int = 100) -> dict:
    """检查磁盘剩余空间。"""
    result = {"ok": True, "detail": ""}
    try:
        usage = shutil.disk_usage(path)
        free_mb = usage.free / (1024 * 1024)
        total_mb = usage.total / (1024 * 1024)
        result["free_mb"] = round(free_mb, 1)
        result["total_mb"] = round(total_mb, 1)

        if free_mb < critical_mb:
            result["ok"] = False
            result["severity"] = "critical"
            result["detail"] = f"disk free {free_mb:.0f}MB < {critical_mb}MB (critical)"
        elif free_mb < warn_mb:
            result["ok"] = False
            result["severity"] = "warn"
            result["detail"] = f"disk free {free_mb:.0f}MB < {warn_mb}MB (warn)"
        else:
            result["detail"] = f"disk OK ({free_mb:.0f}MB free)"
    except OSError as e:
        result["ok"] = False
        result["severity"] = "warn"
        result["detail"] = f"disk check failed: {e}"
    return result


def check_lancedb(db_path: str = "/root/.openclaw/memory/lancedb-pro") -> dict:
    """检查 LanceDB 连通性。"""
    result = {"ok": True, "detail": ""}
    try:
        import lancedb
        db = lancedb.connect(db_path)
        try:
            table = db.open_table("memories")
            _ = table.schema  # 只读 schema，不触发 embedding 加载
            result["detail"] = "LanceDB OK (connected)"
            result["lancedb_ok"] = True
        finally:
            # lancedb 0.34 连接对象无 close()，此处 hasattr 防御：
            # 直接调 db.close() 会抛 AttributeError → 被外层 catch 成误报 warn
            close = getattr(db, "close", None)
            if callable(close):
                close()
    except ImportError:
        result["detail"] = "LanceDB skipped (lancedb not installed)"
        result["lancedb_ok"] = None
    except Exception as e:
        result["ok"] = False
        result["severity"] = "warn"
        result["detail"] = f"LanceDB unreachable: {e}"
        result["lancedb_ok"] = False
    return result


def run_all_checks(state_path: str = None, log_path: str = None) -> dict:
    """运行所有检查，返回汇总结果。"""
    sp = Path(state_path) if state_path else PROJECT_DIR / "chiguo_state.json"
    lp = Path(log_path) if log_path else PROJECT_DIR / "chiguo_decisions.jsonl"

    checks = {
        "daemon_tick": check_daemon_tick(sp),
        "log_freshness": check_log_freshness(lp),
        "disk_space": check_disk_space(PROJECT_DIR),
        "lancedb": check_lancedb(),
    }

    issues = []
    overall = "ok"
    for name, c in checks.items():
        if not c["ok"]:
            issues.append(f"[{name}] {c['detail']}")
            if c.get("severity") == "critical":
                overall = "critical"
            elif c.get("severity") == "warn" and overall != "critical":
                overall = "warn"

    return {
        "overall": overall,
        "healthy": overall == "ok",
        "checks": checks,
        "issues": issues,
        "checked_at": now().isoformat(),
    }


def cli():
    import argparse
    p = argparse.ArgumentParser(description="迟菓 独立看门狗")
    p.add_argument("--quiet", action="store_true", help="仅异常时输出")
    p.add_argument("--notify", action="store_true", help="异常时 stderr 输出告警摘要")
    p.add_argument("--state", default=None, help="状态文件路径")
    p.add_argument("--log", default=None, help="日志文件路径")
    args = p.parse_args()

    result = run_all_checks(args.state, args.log)

    # stderr 通知
    if args.notify and result["issues"]:
        print(f"[chiguo_watchdog] {result['overall'].upper()}:", file=sys.stderr)
        for issue in result["issues"]:
            print(f"  - {issue}", file=sys.stderr)

    # stdout
    if not args.quiet or result["issues"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))

    # 退出码
    if result["overall"] == "critical":
        sys.exit(2)
    elif result["overall"] == "warn":
        sys.exit(1)
    else:
        sys.exit(0)


if __name__ == "__main__":
    os.chdir(PROJECT_DIR)
    cli()
