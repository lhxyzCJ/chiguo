#!/usr/bin/env python3
"""test_schedule_migrations.py — Issue #398: 迁移逻辑独立模块回归测试。

守护点:
1. 迁移函数寄宿于 schedule.migrations(非 ScheduleApi 方法)。
2. 独立模块调用与原内联行为一致(countdown→reminder 一次性迁移 + 幂等)。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import tempfile
from datetime import date
from pathlib import Path

from schedule.api import ScheduleApi
from schedule.migrations import ensure_migrations


def test_migration_functions_live_in_module():
    # ScheduleApi 不再以方法形式承载迁移逻辑
    for name in ("ensure_migrations", "_materialize_anniversaries",
                 "_migrate_countdown", "_migrate_toml_exam_weeks",
                 "_migrate_toml_special_dates"):
        assert not hasattr(ScheduleApi, name), f"ScheduleApi.{name} 应已移至 schedule.migrations"
    import schedule.migrations as m
    for name in ("ensure_migrations",):
        assert callable(getattr(m, name)), f"schedule.migrations.{name} 缺失"


def test_countdown_migration_via_module():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "anniversaries.json").write_text(json.dumps(
            {"anniversaries": [{"type": "countdown", "name": "考研",
                                "date": "2026-12-20"}]}, ensure_ascii=False))
        api = ScheduleApi(td, {"schedule": {}}, today=date(2026, 8, 5))
        ensure_migrations(api)  # 独立模块直调,与 _guard 路径同行为
        labels = [r.get("label") for r in api.overrides.items()]
        assert "考研" in labels, "countdown 应迁入 overrides reminder"
        raw = json.loads(Path(td, "anniversaries.json").read_text())
        assert all(it.get("type") != "countdown" for it in raw["anniversaries"])
        # 幂等:重复执行不重复迁入
        ensure_migrations(api)
        labels2 = [r.get("label") for r in api.overrides.items()]
        assert labels2.count("考研") == 1, "重复迁移不得重复迁入"
