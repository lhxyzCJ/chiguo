#!/usr/bin/env python3
# ============================================================
# chiguo_rotation.py — 日志按月轮转 + 保留策略清理
#
# 零依赖，纯 stdlib。原子 rename，安全并发读。
# 每次 DecisionEngine.__init__() 调用 rotate_if_needed()。
# cron 模式（新进程每次触发）= 每次 cron 都检查一次。
# --loop 模式 = 仅启动时检查（loop 是调试模式，可接受）。
# ============================================================

import json
import os
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from chiguo_time import CST  # Q22: 共享时区常量


def _anchor_archive_dir(archive_dir: str) -> Path:
    """相对 archive_dir 锚定到模块目录；绝对路径原样保留。
    防止从其他 cwd 运行（如 /tmp）时把日志移进 cwd/archive，移出项目真实数据。"""
    p = Path(archive_dir)
    if not p.is_absolute():
        p = Path(__file__).resolve().parent / p
    return p


def _events_log_path() -> Path:
    """轮转事件审计文件（chiguo_events.jsonl），锚定到模块目录（项目根）。
    追加式 JSONL，一行一条：{event, kind, file, at}。供 monitor 时序指标消费。"""
    return Path(__file__).resolve().parent / "chiguo_events.jsonl"


def log_rotation_event(kind: str, filename: str):
    """追加一条轮转事件到 chiguo_events.jsonl。
    事件记录失败静默（不影响轮转主流程）；追加写不设锁（轮转本身低频）。"""
    try:
        line = json.dumps({
            "event": "rotation",
            "kind": kind,                       # monthly | force
            "file": filename,
            "at": datetime.now(CST).isoformat(),
        }, ensure_ascii=False) + "\n"
        with open(_events_log_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 - 事件记账失败不影响轮转
        pass


def rotate_if_needed(log_paths: list[str],
                     config_path: str = "chiguo_proactive.toml"):
    """检测每个日志文件是否需要按月轮转。

    规则：文件 mtime 的月份 != 当前月份 → 移到 archive/ 目录。
    同时清理超过保留期限的归档文件。

    Args:
        log_paths: 日志文件路径列表
        config_path: TOML 配置文件路径
    """
    config = _load_config(config_path)
    retention = config.get("retention_months", 12)
    archive_dir = _anchor_archive_dir(config.get("archive_dir", "archive"))

    now = datetime.now(CST)
    current_month = now.strftime("%Y-%m")

    for log_path in log_paths:
        p = Path(log_path)
        if not p.exists() or p.stat().st_size == 0:
            continue

        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=CST)
        if mtime.strftime("%Y-%m") == current_month:
            continue  # 当月，不轮转

        _rotate_one(p, archive_dir, mtime)

    # 清理过期归档
    _cleanup_archives(archive_dir, retention, now)


def force_rotate(log_paths: list[str],
                 archive_dir: str = "archive"):
    """强制立即轮转，不检查月份。

    Args:
        log_paths: 日志文件路径列表
        archive_dir: 归档目录
    """
    now = datetime.now(CST)
    current_month = now.strftime("%Y-%m")
    archive_path = _anchor_archive_dir(archive_dir)
    archive_path.mkdir(parents=True, exist_ok=True)

    for log_path in log_paths:
        p = Path(log_path)
        if not p.exists() or p.stat().st_size == 0:
            continue
        archive_name = archive_path / f"{current_month}-{p.name}"
        # 防止重复调用覆盖已有归档
        if archive_name.exists():
            stamp = datetime.now(CST).strftime("%Y%m%d%H%M%S%f")
            archive_name = archive_path / f"{current_month}-{p.stem}-{stamp}{p.suffix}"
        try:
            os.rename(str(p), str(archive_name))
            p.touch()
            log_rotation_event("force", str(p))
        except OSError as e:
            print(f"rotation: 强制轮转 {p} 失败: {e}", file=sys.stderr)


def _rotate_one(file_path: Path, archive_dir: str, mtime: datetime):
    """轮转单个文件：move 到归档目录，创建空文件。"""
    archive_path = _anchor_archive_dir(archive_dir)
    archive_path.mkdir(parents=True, exist_ok=True)
    archive_name = archive_path / f"{mtime.strftime('%Y-%m')}-{file_path.name}"
    # 目标已存在 → 追加时间戳后缀，避免静默覆盖旧归档
    if archive_name.exists():
        stamp = datetime.now(CST).strftime("%Y%m%d%H%M%S%f")
        archive_name = archive_path / (
            f"{mtime.strftime('%Y-%m')}-{file_path.stem}-{stamp}{file_path.suffix}"
        )

    try:
        os.rename(str(file_path), str(archive_name))
        file_path.touch()
        log_rotation_event("monthly", str(file_path))
    except OSError as e:
        print(f"rotation: 轮转 {file_path} 失败: {e}", file=sys.stderr)


def _load_config(config_path: str) -> dict:
    """读取 [logging] 段配置。失败返回默认值。"""
    try:
        with open(config_path, "rb") as f:
            cfg = tomllib.load(f)
        return cfg.get("logging", {})
    except Exception:
        return {}


def _cleanup_archives(archive_dir: str, retention_months: int,
                      now: datetime):
    """删除超过保留期限的归档文件。retention_months=0 表示永不删除。"""
    try:
        retention_months = int(retention_months)
    except (TypeError, ValueError):
        retention_months = 12
    if retention_months <= 0:
        return

    cutoff = now - timedelta(days=retention_months * 31)
    archive_path = _anchor_archive_dir(archive_dir)
    if not archive_path.exists():
        return

    for f in archive_path.iterdir():
        if not f.is_file():
            continue
        if f.suffix not in (".jsonl", ".json"):
            continue
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=CST)
            if mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ── CLI ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="日志轮转")
    p.add_argument("--force", action="store_true", help="强制立即轮转")
    p.add_argument("--dry-run", action="store_true", help="仅显示将要轮转的文件")
    args = p.parse_args()

    # Q24 (#275): 轮转名单含对话日志 + 审计日志（chiguo_state_audit.jsonl）。
    # 审计日志不再被明确排除——状态损坏/恢复事件的时间记也要按同一保留策略归档，
    # 保证排查可追溯、不无限增长。轮转事件本身（月轮转/强制轮转）落 chiguo_events.jsonl
    # 供 monitor 时序指标统计（复用 proactive_stats 的每日事件计数）。
    log_files = [
        "chiguo_decisions.jsonl",
        "chiguo_messages.jsonl",
        "chiguo_state_audit.jsonl",
    ]

    if args.force:
        force_rotate(log_files)
        print("强制轮转完成")
    elif args.dry_run:
        config = _load_config("chiguo_proactive.toml")
        now = datetime.now(CST)
        current_month = now.strftime("%Y-%m")
        for lf in log_files:
            p = Path(lf)
            if p.exists():
                mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=CST)
                month = mtime.strftime("%Y-%m")
                if month != current_month:
                    print(f"  ROTATE {lf} (mtime month={month}, current={current_month})")
                else:
                    print(f"  KEEP   {lf} (current month)")
            else:
                print(f"  NONE   {lf} (不存在)")
    else:
        rotate_if_needed(log_files)
        print("轮转检查完成")
