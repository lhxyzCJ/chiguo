#!/usr/bin/env python3
"""test_state_migrations.py — Q6 版本化 migration 表隔离测试

覆盖: 从 v1→v10 各历史版本 state JSON 样本逐一 load，验证被迁移到当前
结构（emotion/cooldown/circadian/personality 均为当前 dataclass 结构，
且各版本引入的迁移字段全部到位）。迁移表 `{from_version: fn}` 幂等有序
应用；数据防御（coerce）与迁移解耦，此处验证迁移路径而非 coerce。
"""

import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_daemon import DecisionEngine  # noqa: E402

TMP_DIR: Path | None = None


def setup() -> Path:
    """复制 toml 到临时目录（隔离 state/log/mem0），返回 cfg 路径。"""
    global TMP_DIR
    TMP_DIR = Path(tempfile.mkdtemp(prefix="chiguo_test_state_migrations_"))
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{TMP_DIR / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{TMP_DIR / "no_history.db"}"', src)
    cfg_path = TMP_DIR / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return cfg_path


def teardown():
    global TMP_DIR
    if TMP_DIR is not None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR = None


def make_engine(cfg_path: Path) -> DecisionEngine:
    return DecisionEngine(str(cfg_path), str(cfg_path.parent / "chiguo_decisions.jsonl"))


@pytest.fixture(scope="module")
def cfg_path():
    """Q26 迁移：setup()/teardown() 逻辑改为 pytest 模块级 fixture（原 __main__ 注入）。"""
    path = setup()
    yield path
    teardown()


# ── 历史版本 state 样本构造 ──────────────────────────────

_FULLV = 10  # 当前 STATE_VERSION

_EMOTION = {"loneliness": 15.0, "affection": 55.0, "anxiety": 40.0,
            "energy": 85.0, "tsundere_index": 70.0}

_PERSONALITY = {"openness": 55.0, "conscientiousness": 65.0,
                "extraversion": 60.0, "agreeableness": 65.0,
                "neuroticism": 60.0, "tsundere_intensity": 70.0,
                "playfulness": 55.0, "attachment_style": 60.0}

_BASELINE = {"openness": 55.0, "conscientiousness": 65.0,
             "extraversion": 60.0, "agreeableness": 65.0,
             "neuroticism": 60.0, "tsundere_intensity": 70.0,
             "playfulness": 55.0, "attachment_style": 60.0}


def _full_snapshot() -> dict:
    """当前（v10）结构的完整 state 样本，作为裁剪基线。"""
    return {
        "_version": _FULLV,
        "emotion": dict(_EMOTION),
        "cooldown": {
            "last_message_at": "2026-06-15T14:00:00+08:00",
            "messages_today": 3,
            "trigger_history": ["follow_up", "weather"],
            "event_timestamps": [
                {"type": "follow_up", "time": "2026-06-15T09:00:00+08:00"},
                {"type": "weather", "time": "2026-06-15T12:00:00+08:00"},
            ],
            "accumulated_lambda": 0.0,
            "last_crash_at": "2026-06-14T09:00:00+08:00",
            "crash_timestamps": [
                "2026-06-14T09:00:00+08:00", "2026-06-13T09:00:00+08:00"],
            "recv_dedup": {"text_sha": "aa11", "at": "2026-06-15T13:00:00+08:00",
                           "analysis": False},
        },
        "circadian": {
            "reply_days": [
                {"date": "2026-07-27", "hours": [10], "bucket": "weekday"},
                {"date": "2026-08-01", "hours": [21], "bucket": "weekend"},
            ],
            "quiet_start": 2, "quiet_end": 7, "confidence": 0.8, "sample_days": 14,
            "weekday_quiet_start": 2, "weekday_quiet_end": 7, "weekday_confidence": 0.8,
            "weekend_quiet_start": 0, "weekend_quiet_end": 8, "weekend_confidence": 0.0,
        },
        "personality": dict(_PERSONALITY),
        "personality_baseline": dict(_BASELINE),
        "personality_history": [
            {"ts": "2026-06-14T09:00:00+08:00", "dims": dict(_BASELINE)},
        ],
        "pending_topics": [
            {"topic": "晚饭", "source": "schedule", "created_at": "2026-06-15T10:00:00+08:00"},
        ],
        "last_tick": "2026-06-15T14:00:00+08:00",
        "tick_seq": 3,
    }


# 各字段引入的版本号：构造历史样本时删除“晚于目标版本”的字段。
# key 为点路径（顶层.嵌套），value 为引入该字段的 STATE_VERSION。
_FIELD_INTRO_VERSION = {
    "cooldown.trigger_history": 2,
    "cooldown.event_timestamps": 3,
    "cooldown.accumulated_lambda": 5,
    "cooldown.crash_timestamps": 6,
    "cooldown.recv_dedup": 9,
    "circadian.weekday_quiet_start": 8,
    "circadian.weekday_quiet_end": 8,
    "circadian.weekday_confidence": 8,
    "circadian.weekend_quiet_start": 8,
    "circadian.weekend_quiet_end": 8,
    "circadian.weekend_confidence": 8,
    "personality_baseline": 10,
    "personality_history": 10,
}


def _snapshot_for(version: int) -> dict:
    """构造目标版本的历史 state 样本：从当前结构裁剪掉该版本之后才引入的字段。

    v1 样本 = 最老结构（缺所有后续版本新字段）；v10 样本 = 当前完整结构。
    """
    snap = _full_snapshot()
    snap["_version"] = version
    for path, intro_ver in _FIELD_INTRO_VERSION.items():
        if intro_ver > version:
            top, _, leaf = path.partition(".")
            if leaf:
                snap[top].pop(leaf, None)
            else:
                snap.pop(top, None)
    return snap


def _load_state(cfg_path: Path, snap: dict):
    """写入 state 文件并重建 engine（触发 _load → 迁移）。返回 engine.state。"""
    s = make_engine(cfg_path).state
    s.state_path.write_text(json.dumps(snap))
    return make_engine(cfg_path).state


# ── 版本组合：v1→v10 逐一 load 迁移到当前结构 ─────────────

def test_all_versions_migrate_to_current_structure(cfg_path: Path):
    """v1→v10 全部版本 state 样本：load 后均为当前 dataclass 结构，
    且被裁剪掉的迁移字段全部补位干净。"""
    for version in range(1, _FULLV + 1):
        snap = _snapshot_for(version)
        st = _load_state(cfg_path, snap)

        # 当前 dataclass 结构（不崩溃 + 基类身份）
        from chiguo_state import ChiguoEmotion, CooldownState, CircadianTracker
        assert isinstance(st.emotion, ChiguoEmotion), version
        assert isinstance(st.cooldown, CooldownState), version
        assert isinstance(st.circadian, CircadianTracker), version
        # pending_topics 为 list（来源样本若存在则应保留，缺失 → 空 list）
        assert isinstance(st.pending_topics, list), version

        # v2→v3: event_timestamps 就位（v≤2 从 trigger_history 推导出同长列表）
        n_hist = len(snap.get("cooldown", {}).get("trigger_history", []))
        if version <= 2:
            assert len(st.cooldown.event_timestamps) == n_hist, version
        else:
            assert len(st.cooldown.event_timestamps) == len(
                _full_snapshot()["cooldown"]["event_timestamps"]), version

        # v6: crash_timestamps 就位（v≤5 从 last_crash_at 恢复单条；v6+ 保留原值）
        if version <= 5:
            assert st.cooldown.crash_timestamps == [
                "2026-06-14T09:00:00+08:00"], version
        else:
            assert st.cooldown.crash_timestamps == [
                "2026-06-14T09:00:00+08:00", "2026-06-13T09:00:00+08:00"], version

        # v8: reply_days 每项带 bucket；v≤7 的单桶窗口继承到 weekday_*
        assert all(d.get("bucket") for d in st.circadian.reply_days), \
            (version, st.circadian.reply_days)
        if version <= 7:
            assert st.circadian.weekday_quiet_start == 2, version
            assert st.circadian.weekday_quiet_end == 7, version
            assert st.circadian.weekday_confidence == 0.8, version
        else:
            assert st.circadian.weekday_quiet_start == 2, version
            assert st.circadian.weekday_quiet_end == 7, version

        # v10: personality 基线（v≥10 恢复持久化基线 == _BASELINE；
        # v≤9 无持久化基线 → 回退 toml 构造初始基线）
        if version >= 10:
            assert st.personality._baseline == _BASELINE, \
                (version, st.personality._baseline)
        else:
            assert st.personality._baseline == st._personality_initial_baseline, \
                (version, st.personality._baseline, st._personality_initial_baseline)

        print(f"  OK v{version} → v{_FULLV} migrated")


def test_v8_bucket_backfill_and_weekday_inherit(cfg_path: Path):
    """v8 迁移的补桶启发式分支：reply_days 无 bucket 条目 → 按日期补桶；
    旧单桶窗口 confidence>0 且 weekday_*/weekend_* 默认 → 继承到 weekday_*。
    （其余版本组合用例因样本已预置 bucket 只覆盖 weekday_* 继承分支。）"""
    snap = _snapshot_for(7)                      # v7：无 weekday_*/bucket
    for d in snap["circadian"]["reply_days"]:    # 显式去掉 bucket → 触发补桶分支
        d.pop("bucket", None)
    # 周六 2026-08-01 → weekend，周一 2026-07-27 → weekday
    snap["circadian"]["reply_days"] = [
        {"date": "2026-07-27", "hours": [10]},   # 周一 → weekday
        {"date": "2026-08-01", "hours": [21]},   # 周六 → weekend
    ]
    st = _load_state(cfg_path, snap)
    by_bucket = {d["bucket"]: d["date"] for d in st.circadian.reply_days}
    assert by_bucket == {"weekday": "2026-07-27", "weekend": "2026-08-01"}, by_bucket
    assert st.circadian.weekday_quiet_start == 2
    assert st.circadian.weekday_quiet_end == 7
    assert st.circadian.weekday_confidence == 0.8
    print("  OK v8 bucket backfill + weekday inherit")
