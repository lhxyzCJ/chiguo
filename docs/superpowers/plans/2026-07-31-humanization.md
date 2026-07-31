# 迟菓拟人化增强实现计划 — 生物钟学习 + 接话茬

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让迟菓根据主人的真实作息动态调整静默窗口(生物钟学习),并在用户话题搁置几小时后自然接续对话(接话茬)。

**Architecture:** 新增 `chiguo_circadian.py` 纯函数模块(作息学习,不依赖状态文件),在 `chiguo_state.py` 中挂载 `CircadianTracker` 与 `pending_topics` 并持久化到 `chiguo_state.json`(STATE_VERSION 6→7),在 `chiguo_trigger.py` 增加 `follow_up` 触发类型(权重随话题年龄钟形调制,单次尝试),`chiguo_daemon.py` 的 `_build_context` 输出接话茬提示给 OpenClaw。全部沿用现有零 LLM 决策引擎架构与测试基建(固定种子 + 固定 CST 时间 + 临时目录隔离)。

**Tech Stack:** Python 3.14 (uv), stdlib only (dataclasses/tomllib/json/math), LanceDB 只读桥接(可选)。

**关键既有模式(必须沿用):**
- 测试:每个 `test_*.py` 独立 runner + 纯 `assert`,失败退出码非零;`tempfile.TemporaryDirectory` 隔离;`random.seed(seed0+i)` 逐次播种;`CST = timezone(timedelta(hours=8))`
- 状态持久化:`save()` 原子写(`.tmp` → `os.replace`) + SHA256 校验和;`_apply_loaded_data` 字段过滤(未知字段不塞 dataclass)
- 路径锚定:`_base_dir`(config 注入),`self._anchored()`
- 运行命令一律从 `/root/character_test` 执行:`uv run python xxx.py`

**Spec:** `docs/superpowers/specs/2026-07-31-humanization-design.md`(v2,已批准)

---

### Task 1: 新建 `chiguo_circadian.py` — 纯函数 + `CircadianTracker` 数据类

**Files:**
- Create: `/root/character_test/chiguo_circadian.py`
- Create: `/root/character_test/test_circadian.py`(纯函数部分;集成部分在 Task 2)

- [ ] **Step 1: 写失败测试**(`test_circadian.py`)

```python
#!/usr/bin/env python3
"""test_circadian.py — chiguo_circadian 生物钟学习单元测试（v7）"""

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))

from chiguo_circadian import (
    estimate_sleep_window, track_reply_hour, aggregate_hours,
    count_sample_days, CircadianTracker,
)


def test_cold_start_returns_none():
    """无数据(0 天)或数据不足(< min_sample_days) → None(回退默认窗口)"""
    counts = [0] * 24
    assert estimate_sleep_window(counts, 0, 14) is None
    assert estimate_sleep_window(counts, 5, 14) is None
    assert estimate_sleep_window(counts, 14, 14, min_sample_days=7) is None
    print("  OK test_cold_start_returns_none")


def test_clear_night_sleep_window():
    """0-5 点零回复,其余每小时 10 条 → 学习窗口 0-5(最小宽度优先)"""
    counts = [0] * 6 + [10] * 18
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["quiet_start"] == 0
    assert est["quiet_end"] == 5  # 不含 end,与 cooldown 窗口语义一致
    assert est["width"] == 5
    assert est["confidence"] == 1.0
    print("  OK test_clear_night_sleep_window")


def test_wrap_midnight_window():
    """22,23,0,1,2 点零回复 → 跨午夜窗口 22-3(qe < qs,端不含)"""
    counts = [0] * 3 + [10] * 19  # 0,1,2 点零
    counts[22] = 0  # 22 点零
    counts[23] = 0  # 23 点零
    # 零回复小时:22,23,0,1,2 → 唯一 sum=0 的宽5窗口:start=22
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["quiet_start"] == 22
    assert est["quiet_end"] == 3  # (22+5) % 24 = 3,窗口含 22,23,0,1,2
    assert est["width"] == 5
    print("  OK test_wrap_midnight_window")


def test_partial_data_confidence_scales():
    """样本天数不足满窗口 → 置信度 = 完整度 × 安静度,低于 1"""
    counts = [0] * 6 + [10] * 18
    est = estimate_sleep_window(counts, 10, 14)
    assert est is not None
    assert est["confidence"] == round(1.0 * (10 / 14), 3)
    print("  OK test_partial_data_confidence_scales")


def test_window_with_activity_lowers_confidence():
    """窗口内有零星活动 → 安静度 < 1,置信度下调但仍可通过阈值"""
    counts = [2] * 8 + [10] * 16  # 0-7 点每小时 2 条
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["width"] == 5
    assert est["quiet_start"] == 0
    assert 0.6 < est["confidence"] < 1.0
    print("  OK test_window_with_activity_lowers_confidence")


def test_invalid_inputs():
    """长度非 24 / 总回复为 0 → None,不崩溃"""
    assert estimate_sleep_window([1, 2, 3], 14, 14) is None
    assert estimate_sleep_window([0] * 24, 14, 14) is None
    print("  OK test_invalid_inputs")


def test_track_reply_hour_append_prune_aggregate():
    """track_reply_hour: 同日追加、跨日新开、过期修剪;aggregate/count 正确"""
    now = datetime(2026, 7, 31, 14, 30, tzinfo=CST)
    days = []
    days = track_reply_hour(days, now, history_days=3)
    days = track_reply_hour(days, now, history_days=3)
    assert len(days) == 1 and days[0]["hours"] == [14, 14]
    days = track_reply_hour(days, now.replace(day=30), history_days=3)
    assert len(days) == 2
    days = track_reply_hour(days, now.replace(day=28), history_days=3)
    # 3 天窗口:7-28 起修剪 → 只剩 7-30、7-31
    assert len(days) == 2, days
    hours = aggregate_hours(days)
    assert len(hours) == 24 and hours[14] == 2 and hours[13] == 1
    assert count_sample_days(days) == 2
    print("  OK test_track_reply_hour_append_prune_aggregate")


def test_tracker_recompute_defaults():
    """CircadianTracker.recompute: 数据不足 → 保持默认 0-8 / confidence 0"""
    tr = CircadianTracker()
    assert tr.recompute() is None
    assert tr.quiet_start == 0 and tr.quiet_end == 8 and tr.confidence == 0.0
    tr.reply_days = [{"date": "2026-07-31", "hours": list(range(9, 23))}]
    tr.recompute(min_sample_days=7, history_days=14)
    assert tr.sample_days == 1
    assert tr.confidence == 0.0  # 未达标不写窗口
    print("  OK test_tracker_recompute_defaults")


if __name__ == "__main__":
    test_cold_start_returns_none()
    test_clear_night_sleep_window()
    test_wrap_midnight_window()
    test_partial_data_confidence_scales()
    test_window_with_activity_lowers_confidence()
    test_invalid_inputs()
    test_track_reply_hour_append_prune_aggregate()
    test_tracker_recompute_defaults()
    print("test_circadian.py: ALL PASS")
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /root/character_test && uv run python test_circadian.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'chiguo_circadian'`

- [ ] **Step 3: 实现 `chiguo_circadian.py`**

```python
# ============================================================
# chiguo_circadian.py — 生物钟学习 v7
# 从主人回复时间学习睡眠/活跃时段,动态调整静默窗口
# 纯函数为主:可独立测试,不依赖状态文件
# ============================================================

from dataclasses import dataclass, field
from datetime import datetime, timedelta

MIN_HOURS = 24


def estimate_sleep_window(hour_counts: list[int], sample_days: int,
                          history_days: int, min_sample_days: int = 7,
                          min_width: int = 5, max_width: int = 12) -> dict | None:
    """
    从 24 小时回复频率中估计睡眠窗口(允许跨午夜)。

    算法:对 24 小时做环形滑动窗口(width ∈ [min_width, max_width]),
    取回复总数最小的窗口;(sum, width, start) 元组最小者胜(字典序:
    总数最小 → 宽度最小 → 起点最早),保证确定性。

    置信度 = 数据完整度(sample_days/history_days) × 窗口安静度
    (1 - 窗口均回复/全天均回复)。

    返回 {"quiet_start", "quiet_end", "width", "confidence", "sample_days"}
    quiet_end 不含 end(与 cooldown 窗口语义一致,qe < qs 表示跨午夜)。
    数据不足/无数据 → None(调用方回退配置默认窗口)。
    """
    if len(hour_counts) != MIN_HOURS:
        return None
    if sample_days < min_sample_days:
        return None
    total = sum(hour_counts)
    if total <= 0:
        return None

    best: tuple | None = None  # (sum, width, start)
    for width in range(min_width, max_width + 1):
        for start in range(MIN_HOURS):
            s = sum(hour_counts[(start + i) % MIN_HOURS] for i in range(width))
            key = (s, width, start)
            if best is None or key < best:
                best = key

    sum_w, width, start = best
    end = (start + width) % MIN_HOURS
    avg_hour = total / MIN_HOURS
    window_mean = sum_w / width
    quietness = 1.0 - min(1.0, window_mean / max(avg_hour, 1e-9))
    completeness = min(1.0, sample_days / max(1, history_days))
    confidence = round(completeness * quietness, 3)
    return {
        "quiet_start": start,
        "quiet_end": end,
        "width": width,
        "confidence": confidence,
        "sample_days": sample_days,
    }


def track_reply_hour(reply_days: list[dict], now: datetime,
                     history_days: int) -> list[dict]:
    """记录一次回复的小时。返回滚动更新后的 reply_days(保留最近 history_days 天)。"""
    today = now.strftime("%Y-%m-%d")
    for d in reply_days:
        if d.get("date") == today:
            d["hours"].append(now.hour)
            break
    else:
        reply_days.append({"date": today, "hours": [now.hour]})
    cutoff = (now - timedelta(days=history_days)).strftime("%Y-%m-%d")
    return [d for d in reply_days if d.get("date", "") >= cutoff]


def aggregate_hours(reply_days: list[dict]) -> list[int]:
    """汇总 reply_days → 24 小时计数数组(长度恒 24)。"""
    counts = [0] * MIN_HOURS
    for d in reply_days:
        for h in d.get("hours", []):
            try:
                counts[int(h)] += 1
            except (ValueError, TypeError, IndexError):
                continue
    return counts


def count_sample_days(reply_days: list[dict]) -> int:
    """有回复记录的天数。"""
    return sum(1 for d in reply_days if d.get("hours"))


@dataclass
class CircadianTracker:
    """作息学习器。存 chiguo_state.json 的 "circadian" 字段。"""
    reply_days: list[dict] = field(default_factory=list)  # [{"date": "YYYY-MM-DD", "hours": [0-23,...]}]
    quiet_start: int = 0    # 学习到的睡眠窗口起点(未达标时保持默认)
    quiet_end: int = 8      # 学习到的睡眠窗口终点(不含)
    confidence: float = 0.0  # 学习置信度(0-1)
    sample_days: int = 0     # 有数据的天数

    def record(self, now: datetime, history_days: int = 14):
        """记录一次回复时间。"""
        self.reply_days = track_reply_hour(self.reply_days, now, history_days)

    def recompute(self, min_sample_days: int = 7, history_days: int = 14,
                  min_width: int = 5, max_width: int = 12) -> dict | None:
        """重算学习窗口。数据不足 → 不覆盖当前值,返回 None。"""
        self.sample_days = count_sample_days(self.reply_days)
        est = estimate_sleep_window(
            aggregate_hours(self.reply_days), self.sample_days,
            history_days, min_sample_days, min_width, max_width,
        )
        if est is None:
            return None
        self.quiet_start = est["quiet_start"]
        self.quiet_end = est["quiet_end"]
        self.confidence = est["confidence"]
        return est
```

- [ ] **Step 4: 运行验证通过**

Run: `cd /root/character_test && uv run python test_circadian.py`
Expected: PASS — `test_circadian.py: ALL PASS`

- [ ] **Step 5: 提交**

本目录非 git 仓库,无需 commit(项目规范:用 MEMORY.md 记录改动,最终 Task 7 统一记录)。

---

### Task 2: state 集成 — circadian 持久化 + 动态静默窗口应用

**Files:**
- Modify: `/root/character_test/chiguo_state.py`(`__init__`/`on_user_message`/`save`/`_apply_loaded_data`/STATE_VERSION/新增 `_sync_quiet_window`)
- Modify: `/root/character_test/test_circadian.py`(追加集成测试)

- [ ] **Step 1: 追加失败测试**(`test_circadian.py` 集成部分)

```python
import tomllib
import json
from pathlib import Path
from chiguo_state import ChiguoState
from chiguo_circadian import CircadianTracker


def _make_state(tmp: str) -> ChiguoState:
    """真实 toml 配置 + 临时目录锚定;lancedb 指向不存在路径(确定性)"""
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["lancedb_path"] = str(Path(tmp) / "no_lancedb")
    return ChiguoState(cfg)


def test_state_records_and_persists_circadian():
    """on_user_message → reply_days 追加;save/load 往返不丢失"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 14, 0, tzinfo=CST)
        s = _make_state(td)
        s.on_user_message(now)
        s.on_user_message(now.replace(hour=22))
        assert s.circadian.reply_days[0]["hours"] == [14, 22]
        s.save(_backup=False, _increment_tick=False)

        s2 = _make_state(td)  # 重新加载
        assert s2.circadian.reply_days == [{"date": "2026-07-31", "hours": [14, 22]}]
    print("  OK test_state_records_and_persists_circadian")


def test_quiet_window_uses_learned_window_when_confident():
    """置信度高 → cooldown 睡眠窗口 = 学习窗口(替代固定 0-8)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        # 注入 14 天、夜间零回复的数据 → 置信度 1.0
        s.circadian.reply_days = [
            {"date": f"2026-07-{d:02d}", "hours": list(range(9, 24))}
            for d in range(18, 32)
        ]
        s.circadian.recompute(history_days=14)
        assert s.circadian.confidence == 1.0
        s._sync_quiet_window()
        assert s.cooldown._quiet_start == 0  # 0-4 零回复 → 窗口 0-5
        assert s.cooldown._quiet_end == 5
        # 睡眠时间不算真沉默:10h 墙钟沉默(含 0-5 共 4h 睡眠) → silent≈6
        s.cooldown.last_user_message_at = (
            datetime(2026, 7, 31, 4, 0, tzinfo=CST).isoformat()
        )
        from datetime import timedelta
        now = datetime(2026, 7, 31, 14, 0, tzinfo=CST)
        sil = s.cooldown.silent_hours(now)
        assert 5.5 < sil < 6.5, sil
    print("  OK test_quiet_window_uses_learned_window_when_confident")


def test_quiet_window_falls_back_when_not_confident():
    """置信度不足(冷启动) → 回退配置默认窗口 0-8,行为与 v6 完全一致"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s._sync_quiet_window()
        assert s.cooldown._quiet_start == 0
        assert s.cooldown._quiet_end == 8
    print("  OK test_quiet_window_falls_back_when_not_confident")


def test_state_old_version_without_circadian_loads():
    """v6 及更早的 state 文件(无 circadian 字段) → 默认值,不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        payload = {"_version": 6, "emotion": {}, "cooldown": {}, "last_tick": "2026-07-31T10:00:00+08:00"}
        (s.state_path).write_text(json.dumps(payload))
        s2 = _make_state(td)
        assert s2.circadian.reply_days == []
        assert s2.circadian.quiet_start == 0 and s2.circadian.quiet_end == 8
        assert s2.cooldown._quiet_start == 0 and s2.cooldown._quiet_end == 8
    print("  OK test_state_old_version_without_circadian_loads")
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /root/character_test && uv run python test_circadian.py`
Expected: FAIL — `AttributeError: 'ChiguoState' object has no attribute 'circadian'`

- [ ] **Step 3: 实现 state 集成**

`chiguo_state.py` 修改(6 处):

1. 头部 import(在 `from memory_bridge import MemoryBridge` 之后加):

```python
from chiguo_circadian import CircadianTracker
```

2. `__init__` 中 `self.cooldown = CooldownState()` 之后加:

```python
        # ── v7: 生物钟学习器(作息数据 + 学习到的睡眠窗口)──
        self.circadian = CircadianTracker()
```

3. 新增方法(放在 `_apply_quiet_window` 之后):

```python
    def _sync_quiet_window(self):
        """v7: 生物钟学习置信度达标 → 用学习到的睡眠窗口;否则回退配置默认。"""
        cfg = self.config.get("circadian", {})
        if self.circadian.confidence >= cfg.get("min_confidence", 0.6):
            self.cooldown.set_quiet_window(self.circadian.quiet_start,
                                           self.circadian.quiet_end)
        else:
            self._apply_quiet_window()
```

4. `on_user_message` 末尾(`self.emotion.clamp()` 之前)追加:

```python
        # ── v7: 生物钟学习(每次回复记录小时 + 重算窗口)──
        circ_cfg = self.config.get("circadian", {})
        self.circadian.record(now, circ_cfg.get("history_days", 14))
        self.circadian.recompute(
            min_sample_days=circ_cfg.get("min_sample_days", 7),
            history_days=circ_cfg.get("history_days", 14),
            min_width=circ_cfg.get("min_width", 5),
            max_width=circ_cfg.get("max_width", 12),
        )
        self._sync_quiet_window()
```

5. `save()` payload 构造(在 `"cooldown": asdict(self.cooldown),` 后加):

```python
                "circadian": asdict(self.circadian),
```

需要 `from dataclasses import asdict` — 已在文件顶部导入(dataclass, asdict, field)。

6. `_apply_loaded_data`:替换 `self._apply_quiet_window()` 那一行(在 crash_timestamps 迁移之后):

```python
        circ_fields = {k: v for k, v in data.get("circadian", {}).items()
                       if k in CircadianTracker.__dataclass_fields__}
        self.circadian = CircadianTracker(**circ_fields)
        self._sync_quiet_window()
```

7. `STATE_VERSION = 6` → `STATE_VERSION = 7  # v7: 生物钟学习(circadian) + 接话茬(pending_topics)`

注意:原 `_apply_loaded_data` 中 `self._apply_quiet_window()` 行被替换为 `self._sync_quiet_window()`,配置默认回退路径仍走 `_apply_quiet_window`。

- [ ] **Step 4: 运行验证通过**

Run: `cd /root/character_test && uv run python test_circadian.py`
Expected: PASS — `test_circadian.py: ALL PASS`

- [ ] **Step 5: 回归防漏(睡眠窗口涉及全局行为)**

Run: `cd /root/character_test && uv run python test_trigger.py && uv run python test_escape_valve.py && uv run python test_integration.py`
Expected: 全过(冷启动置信度 0 → 行为与 v6 完全一致,不应有回归)

---

### Task 3: `pending_topics` 状态 — analysis 话题摄入 + 持久化

**Files:**
- Modify: `/root/character_test/chiguo_state.py`(`__init__`/`on_user_message`/`save`/`_apply_loaded_data`/新增 5 个方法)
- Create: `/root/character_test/test_followup.py`(本 Task 测状态层;触发层在 Task 4)

- [ ] **Step 1: 写失败测试**(`test_followup.py`)

```python
#!/usr/bin/env python3
"""test_followup.py — 接话茬(pending_topics + follow_up 触发)测试(v7)"""

import os
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState


def _make_state(tmp: str, now: datetime) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["lancedb_path"] = str(Path(tmp) / "no_lancedb")
    s = ChiguoState(cfg)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    return s


def test_analysis_topic_appends_and_dedupes():
    """带 topic 的分析 → 追加 pending;同话题再来 → 视为接续,移除旧条目重新计时"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.on_user_message(10, analysis={"topic": "比赛"})
        assert len(s.pending_topics) == 1
        assert s.pending_topics[0]["topic"] == "比赛"
        assert s.pending_topics[0]["source"] == "analysis"
        assert s.pending_topics[0]["attempted"] is False
        # 同话题(继续聊) → 移除旧条目,新条目 created_at 刷新
        s.on_user_message(10, analysis={"topic": "比赛"})
        assert len(s.pending_topics) == 1
        assert s.pending_topics[0]["created_at"] == now.isoformat()
        # 不同话题 → 追加第二条
        s.on_user_message(10, analysis={"topic": "电影"})
        assert len(s.pending_topics) == 2
    print("  OK test_analysis_topic_appends_and_dedupes")


def test_topic_resolved_removes():
    """topic_resolved=true → 移除对应话题(指定 topic 按匹配,未指定移除最旧)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.on_user_message(10, analysis={"topic": "比赛"})
        s.on_user_message(10, analysis={"topic": "电影"})
        s.on_user_message(10, analysis={"topic": "比赛", "topic_resolved": True})
        assert [t["topic"] for t in s.pending_topics] == ["电影"]
        s.on_user_message(10, analysis={"topic_resolved": True})
        assert s.pending_topics == []
    print("  OK test_topic_resolved_removes")


def test_invalid_topic_ignored():
    """topic 非字符串/空 → 忽略不崩;analysis 为 None → 不产生话题"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.on_user_message(10, analysis={"topic": 123})
        s.on_user_message(10, analysis={"topic": "  "})
        s.on_user_message(10, analysis={"topic": "    "})
        s.on_user_message(10, analysis=None)
        assert s.pending_topics == []
    print("  OK test_invalid_topic_ignored")


def test_prune_expired_and_attempted():
    """prune_pending_topics: 过期(>48h)/已尝试 → 移除;上限 20 丢弃最旧"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.add_pending_topic("新鲜的", now - timedelta(hours=3))
        s.add_pending_topic("过期的", now - timedelta(hours=50))
        s.add_pending_topic("已尝试的", now - timedelta(hours=5))
        s.mark_pending_topic_attempted("已尝试的")
        s.prune_pending_topics(now)
        assert [t["topic"] for t in s.pending_topics] == ["新鲜的"]
        # 上限 20
        for i in range(25):
            s.add_pending_topic(f"t{i}", now)
        assert len(s.pending_topics) == 20
        assert s.pending_topics[0]["topic"] == "t5"  # 最旧 5 条被丢弃
    print("  OK test_prune_expired_and_attempted")


def test_pending_topics_persist():
    """pending_topics 存 chiguo_state.json,save/load 往返不丢失"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.add_pending_topic("比赛", now)
        s.save(_backup=False, _increment_tick=False)
        s2 = _make_state(td, now)
        assert [t["topic"] for t in s2.pending_topics] == ["比赛"]
        assert s2.pending_topics[0]["source"] == "analysis"
        assert s2.pending_topics[0]["attempted"] is False
    print("  OK test_pending_topics_persist")


if __name__ == "__main__":
    test_analysis_topic_appends_and_dedupes()
    test_topic_resolved_removes()
    test_invalid_topic_ignored()
    test_prune_expired_and_attempted()
    test_pending_topics_persist()
    print("test_followup.py (state layer): ALL PASS")
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /root/character_test && uv run python test_followup.py`
Expected: FAIL — `AttributeError: 'ChiguoState' object has no attribute 'pending_topics'`

- [ ] **Step 3: 实现 pending_topics**

`chiguo_state.py` 修改(4 处):

1. `__init__` 中 `self.memories: list[dict] = []` 之后加:

```python
        # ── v7: 待接续话题(接话茬)。[{topic, source, created_at, attempted}] ──
        self.pending_topics: list[dict] = []
```

2. 新增方法(放在 `on_user_message` 之前):

```python
    # ── v7: 接话茬 — 待接续话题管理 ────────────────────

    def add_pending_topic(self, topic: str, now: datetime, source: str = "analysis"):
        """记录待接续话题。同话题视为已接续 → 移除旧条目后重新计时(活跃对话不触发)。
        非字符串/空白 → 忽略。上限 20 条,超出丢弃最旧。"""
        if not isinstance(topic, str) or not topic.strip():
            return
        topic = topic.strip()[:50]
        self.pending_topics = [
            t for t in self.pending_topics if t.get("topic") != topic
        ]
        self.pending_topics.append({
            "topic": topic,
            "source": source,
            "created_at": now.isoformat(),
            "attempted": False,
        })
        self._cap_pending_topics()

    def resolve_pending_topic(self, topic: str | None, now: datetime):
        """topic_resolved=true → 移除对应话题。未指定 topic → 移除最旧一条。"""
        if isinstance(topic, str) and topic.strip():
            self.pending_topics = [
                t for t in self.pending_topics if t.get("topic") != topic.strip()
            ]
        elif self.pending_topics:
            self.pending_topics.pop(0)

    def mark_pending_topic_attempted(self, topic: str):
        """接话茬触发后标记已尝试(该话题不再重复触发)。"""
        for t in self.pending_topics:
            if t.get("topic") == topic:
                t["attempted"] = True

    def prune_pending_topics(self, now: datetime, max_age_hours: float = 48.0):
        """移除过期/已尝试话题,防状态膨胀。坏时间戳直接丢弃。"""
        kept = []
        for t in self.pending_topics:
            try:
                dt = datetime.fromisoformat(t.get("created_at", ""))
            except (ValueError, TypeError):
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=CST)
            age = (now - dt).total_seconds() / 3600
            if age <= max_age_hours and not t.get("attempted"):
                kept.append(t)
        self.pending_topics = kept
        self._cap_pending_topics()

    def _cap_pending_topics(self, cap: int = 20):
        if len(self.pending_topics) > cap:
            self.pending_topics = self.pending_topics[-cap:]
```

3. `on_user_message` 中 `self._apply_emotion_impact(analysis, now)` 之后加(Task 2 的生物钟块之前):

```python
        # ── v7: 接话茬话题摄入 ──
        try:
            topic = analysis.get("topic")
            if analysis.get("topic_resolved"):
                self.resolve_pending_topic(topic, now)
            elif topic:
                self.add_pending_topic(topic, now)
        except Exception:
            pass
```

4. `save()` payload 加 `"pending_topics": self.pending_topics,`(circadian 行之后);`_apply_loaded_data` 加(在 circadian 块之后):

```python
        pending = data.get("pending_topics")
        self.pending_topics = pending if isinstance(pending, list) else []
```

- [ ] **Step 4: 运行验证通过**

Run: `cd /root/character_test && uv run python test_followup.py`
Expected: PASS — `test_followup.py (state layer): ALL PASS`

---

### Task 4: `follow_up` 触发类型

**Files:**
- Modify: `/root/character_test/chiguo_trigger.py`(import math + 候选收集 + 选中标记)
- Modify: `/root/character_test/test_followup.py`(追加触发层测试)

- [ ] **Step 1: 写失败测试**(追加到 `test_followup.py`)

```python
from chiguo_trigger import evaluate_triggers, Trigger


def _run_seeds(s: ChiguoState, now: datetime, n: int = 300, seed0: int = 5000) -> dict:
    counts: dict[str, int] = {}
    for i in range(n):
        random.seed(seed0 + i)
        t = evaluate_triggers(s, now)
        key = t.type if t else "None"
        counts[key] = counts.get(key, 0) + 1
    return counts


def test_follow_up_fires_in_age_window():
    """2-48h 内的 pending 话题 → follow_up 候选(权重足够 → 恒触发)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.energy = 40  # 排除 playful 干扰
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        counts = _run_seeds(s, now)
        assert counts.get("follow_up", 0) >= 1, counts
    print("  OK test_follow_up_fires_in_age_window")


def test_follow_up_single_attempt():
    """触发后标记 attempted → 不再重复触发(300 次评估 0 次)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.energy = 40
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        random.seed(42)
        t = evaluate_triggers(s, now)
        assert t is not None and t.type == "follow_up"
        assert s.pending_topics[0]["attempted"] is True
        counts = _run_seeds(s, now)
        assert counts.get("follow_up", 0) == 0, counts
    print("  OK test_follow_up_single_attempt")


def test_follow_up_outside_age_window():
    """年龄 < 2h(刚聊完)或 > 48h(过期) → 不触发"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.energy = 40
        s.add_pending_topic("太新", now - timedelta(minutes=30))
        assert _run_seeds(s, now).get("follow_up", 0) == 0
        s.add_pending_topic("太旧", now - timedelta(hours=50))
        assert _run_seeds(s, now).get("follow_up", 0) == 0
    print("  OK test_follow_up_outside_age_window")


def test_follow_up_data_fields():
    """context 数据: topic/source/age_hours 正确"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.energy = 40
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        random.seed(42)
        t = evaluate_triggers(s, now)
        assert t.data["topic"] == "比赛"
        assert t.data["source"] == "analysis"
        assert 3.5 <= t.data["age_hours"] <= 4.5
    print("  OK test_follow_up_data_fields")


def test_follow_up_expired_pruned():
    """过期话题在评估时被清理(pending_topics 不再残留)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.energy = 40
        s.add_pending_topic("过期的", now - timedelta(hours=49))
        _run_seeds(s, now, n=3)
        assert s.pending_topics == []
    print("  OK test_follow_up_expired_pruned")
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /root/character_test && uv run python test_followup.py`
Expected: FAIL — `test_follow_up_fires_in_age_window` 断言失败(follow_up 计数为 0)

- [ ] **Step 3: 实现 follow_up 触发**

`chiguo_trigger.py` 修改(3 处):

1. 头部 import 加 `import math`:

```python
import random
import math
from datetime import datetime
```

2. LanceDB 记忆候选块之后、`# ── 情绪驱动事件 ──` 之前插入:

```python
    # ── v7: 接话茬(follow_up)触发 ──
    # 待接续话题(analysis topic,2-48h 内)优先;无待接续话题 → 近期用户相关记忆兜底。
    # 权重 = follow_up_weight × 年龄钟形(峰值 follow_up_peak_hours)。
    # 触发后标记 attempted(单次尝试);过期话题顺带清理。
    trg_cfg = state.config.get("trigger", {})
    fup_min = trg_cfg.get("follow_up_min_age_hours", 2.0)
    fup_max = trg_cfg.get("follow_up_max_age_hours", 48.0)
    state.prune_pending_topics(now, fup_max)
    follow_entry = None
    for t in state.pending_topics:
        try:
            dt = datetime.fromisoformat(t.get("created_at", ""))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        age = (now - dt).total_seconds() / 3600
        if fup_min <= age <= fup_max:
            follow_entry = (t, age)
            break
    if follow_entry is None and not state.pending_topics and state.memory_bridge.available:
        # 记忆兜底:近 48h 内、用户相关的记忆,选中一条作为接话茬素材(不落盘)
        now_ts = now.timestamp()
        for mem in state.memory_bridge.user_relevant(limit=10, min_importance=0.4):
            ts = mem.get("timestamp") or 0
            if not ts:
                continue
            ts = ts / 1000.0 if ts > 1e12 else ts  # epoch ms → s
            age = (now_ts - ts) / 3600
            if 0 < age <= fup_max:
                text = (mem.get("l0_abstract") or mem.get("text") or "").strip()[:50]
                if text:
                    follow_entry = (
                        {"topic": text, "source": "memory",
                         "created_at": now.isoformat()},
                        age,
                    )
                    break
    if follow_entry is not None:
        entry, age = follow_entry
        peak = trg_cfg.get("follow_up_peak_hours", 4.0)
        sigma = trg_cfg.get("follow_up_sigma_hours", 3.0)
        bell = math.exp(-((age - peak) / sigma) ** 2)
        w = trg_cfg.get("follow_up_weight", 0.35) * bell
        if w > trg_cfg.get("follow_up_min_weight", 0.03):
            weighted_candidates.append({
                "trigger": Trigger(type="follow_up", intensity="soft",
                                   data={"topic": entry["topic"],
                                         "source": entry["source"],
                                         "age_hours": round(age, 1)},
                                   description=f"接话茬: {entry['topic'][:20]}"),
                "weight": w,
                "topic_ref": entry,
            })
```

3. 选中之后(在 `trigger = chosen["trigger"]` 之后、安全阀降级之前)插入:

```python
    # ── v7: 接话茬触发后标记已尝试(防重复;记忆兜底条目不在 pending 中,no-op)──
    if trigger.type == "follow_up" and chosen.get("topic_ref") is not None:
        state.mark_pending_topic_attempted(chosen["topic_ref"].get("topic", ""))
```

- [ ] **Step 4: 运行验证通过**

Run: `cd /root/character_test && uv run python test_followup.py`
Expected: PASS — `test_followup.py: ALL PASS`

- [ ] **Step 5: 回归(trigger 竞争关系变化)**

Run: `cd /root/character_test && uv run python test_trigger.py && uv run python test_integration.py && uv run python test_topics.py`
Expected: 全过(无 pending 话题时候选集不变)

---

### Task 5: daemon context + toml 配置

**Files:**
- Modify: `/root/character_test/chiguo_daemon.py`(`_build_context` + `_maybe_reload_config` 的 `_apply_quiet_window` → `_sync_quiet_window`)
- Modify: `/root/character_test/chiguo_proactive.toml`(`[circadian]` 段 + `[trigger]` follow_up 键)

- [ ] **Step 1: toml 配置**

`chiguo_proactive.toml` 修改(2 处):

1. `[trigger]` 段(`anxiety_min_weight = 0.3` 之后)加:

```toml
# ── v7: 接话茬(follow_up)参数 ──
follow_up_weight = 0.35         # 基础权重(乘年龄钟形调制)
follow_up_min_age_hours = 2.0   # 话题最早可接续年龄(小时)
follow_up_max_age_hours = 48.0  # 超过此年龄过期清理(小时)
follow_up_peak_hours = 4.0      # 钟形权重峰值年龄(小时)
follow_up_sigma_hours = 3.0     # 钟形宽度(小时)
follow_up_min_weight = 0.03     # 低于此权重不成为候选
```

2. 新增段(放在 `[schedule]` 之后、`[hawkes]` 之前):

```toml
[circadian]
# ── v7: 生物钟学习 — 从主人回复时间学习睡眠时段,动态调整静默窗口 ──
history_days = 14        # 回复记录滚动窗口(天)
min_sample_days = 7      # 最少有数据天数才计算学习窗口
min_confidence = 0.6     # 学习置信度低于此值 → 回退配置默认窗口(0-8)
min_width = 5            # 学习窗口最小宽度(小时)
max_width = 12           # 学习窗口最大宽度(小时)
```

- [ ] **Step 2: 写失败测试**(`test_followup.py` 追加 daemon 集成测试)

```python
import subprocess


def test_daemon_context_contains_follow_up_hint():
    """daemon 决策:follow_up 触发 → context 带 follow_up 字段 + 指令含接话茬"""
    with tempfile.TemporaryDirectory() as td:
        # 用真实 state + 注入 pending 话题
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.energy = 40
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        s.save(_backup=False, _increment_tick=False)
        # 通过 daemon --loop 单次评估不可行(cron 真实时间) → 直接调 DecisionEngine
        import chiguo_daemon
        engine = chiguo_daemon.DecisionEngine(str(Path(td) / "chiguo_proactive.toml"))
        # 注入同样的状态文件(engine 自己会 load)
        engine.state.add_pending_topic("比赛", now - timedelta(hours=4))
        engine.state.cooldown.energy = 40
        trigger = evaluate_triggers(engine.state, now)
        # 16:00 无仪式候选 + energy=40 排除 playful + 4h 话题 → follow_up 为唯一候选
        assert trigger is not None and trigger.type == "follow_up", trigger
        context = engine._build_context(trigger, now)
        assert context["follow_up"]["topic"] == "比赛"
        assert "接话茬" in context["instruction"]
        assert "比赛" in context["instruction"]
    print("  OK test_daemon_context_contains_follow_up_hint")
```

- [ ] **Step 3: 运行验证失败**

Run: `cd /root/character_test && uv run python test_followup.py`
Expected: FAIL — `KeyError: 'follow_up'`(context 无该字段)

- [ ] **Step 4: 实现 daemon context + 热重载同步**

`chiguo_daemon.py` 修改(3 处):

1. `_maybe_reload_config` 中 `self.state._apply_quiet_window()` → `self.state._sync_quiet_window()`(注释同步改:v6 修复 → v7 生物钟同步)

2. `_build_context` 中 `safety_note` 之后(角色铁律之前)插入:

```python
        # ── v7: 接话茬提示 ──
        if trigger.type == "follow_up":
            tpc = trigger.data.get("topic", "")
            src = trigger.data.get("source", "analysis")
            age = trigger.data.get("age_hours", 0)
            guidance += (
                f"\n【接话茬】约{age:.0f}小时前和主人聊到「{tpc}」"
                f"(来源:{'对话分析' if src == 'analysis' else '回忆'}),"
                "后来没有下文。像真人突然想起一样自然接续这个话题——"
                "聊天式提起,不要汇报腔,不要生硬转场。"
            )
```

3. `_build_context` return 语句之前(在 `instruction` 构造之后、topic_data 注入块之后)插入:

```python
        # ── v7: 接话茬素材注入(供 OpenClaw 生成)──
        if trigger.type == "follow_up":
            instruction += (
                f"\n用「{trigger.data.get('topic', '')}」这个之前没聊完的话题自然接话茬,"
                "不要直接说『你上次说的那个……后来怎么样了』这种汇报句,"
                "像想起一样顺嘴问。"
            )
```

4. return 字典加键(在 `"accumulated_lambda"` 行之后):

```python
            "follow_up": {
                "topic": trigger.data.get("topic", ""),
                "source": trigger.data.get("source", ""),
                "age_hours": trigger.data.get("age_hours", 0),
            },
```

- [ ] **Step 5: 运行验证通过**

Run: `cd /root/character_test && uv run python test_followup.py`
Expected: PASS — `test_followup.py: ALL PASS`

- [ ] **Step 6: toml 冒烟(热重载/新段不破坏)**

Run: `cd /root/character_test && uv run python chiguo_daemon.py --status`
Expected: 正常输出 JSON 到 stdout,无异常;`uv run python test_integration.py` 全过

---

### Task 6: OpenClaw skill 更新(analysis 字段 + follow_up 处理)

**Files:**
- Modify: `/root/.openclaw/workspace/skills/chiguo/SKILL.md`(安全边界内,允许修改)

- [ ] **Step 1: 更新分析 JSON 规范**

`SKILL.md` 第二节「1. 分析情绪」中 JSON 示例与说明改为:

```markdown
输出 JSON：
```json
{"warmth": -1.0~1.0, "effort": 0.0~1.0, "attention": 0.0~1.0, "suppress_hours": 0.0~24.0, "topic": "话题关键词(可选)", "topic_resolved": false}
```
- warmth: -1.0=敌意/烦躁, 0=中性, 1.0=温暖/亲近
- effort: 0.0=敷衍("嗯"), 0.5=一般, 1.0=用心长消息
- attention: 0.0=无视迟菓, 1.0=直接回应她
- suppress_hours: 可选。检测到"忙"/"晚安"时设置，否则省略
- topic: 可选。本条消息的核心话题关键词(如"比赛""电影""论文")。仅当消息有明确、可延续的具体话题时填写;纯寒暄/纯情绪/无话题时省略
- topic_resolved: 可选 bool。本条消息是否把之前聊到一半的话题聊完了(如对方回答了结果/明确说结束/话题自然终结)。未聊完时省略或 false
```

- [ ] **Step 2: 更新主动消息处理表**

`SKILL.md` 第一节「3. 生成消息」字段表后追加说明:

```markdown
**若 `trigger_type: "follow_up"` → context 含 `follow_up` 字段**（topic/source/age_hours），指令会带【接话茬】提示：像真人想起没聊完的事一样自然接续该话题。聊天式提起（「对了，你那个比赛后来怎么样了」），禁止汇报腔、禁止生硬转场、禁止复读整句指令。
```

- [ ] **Step 3: 验证**

Run: `grep -n "topic_resolved\|follow_up\|接话茬" /root/.openclaw/workspace/skills/chiguo/SKILL.md`
Expected: 3 处命中(JSON 示例 topic/topic_resolved、follow_up 说明、接话茬提示)

---

### Task 7: 文档同步 + 全量回归

**Files:**
- Modify: `/root/character_test/doc/SYSTEM.md`、`/root/character_test/doc/IMPROVE.md`、`/root/character_test/doc/README.md`
- Modify: `/root/character_test/MEMORY.md`、`/root/character_test/CLAUDE.md`、`/root/character_test/AGENTS.md`

- [ ] **Step 1: 更新架构文档**

`doc/SYSTEM.md`:
- 模块依赖图加 `chiguo_circadian.py → 作息学习(滚动窗口 → 睡眠时段 + 置信度)`
- 标题版本 v4 → v4 后补 v7 说明;STATE_VERSION 6→7
- 触发类型 12 种 → 13 种(follow_up)
- 配置段加 `[circadian]` 表 + `[trigger]` follow_up 键
- 新增"生物钟学习"与"接话茬"小节(数据流/降级语义,照 spec §三、§四)

`doc/README.md`:模块列表加 chiguo_circadian.py。

`doc/IMPROVE.md`:新增记录节「v7 拟人化:生物钟学习 + 接话茬」。

- [ ] **Step 2: 更新测试文档**

`CLAUDE.md` 与 `AGENTS.md` 的测试命令链追加两个文件:

```bash
uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && \
uv run python test_integration.py && uv run python test_monitor.py && \
uv run python test_eventbus.py && uv run python test_personality.py && \
uv run python test_bayesian.py && uv run python test_composer.py && \
uv run python test_ebbinghaus.py && uv run python test_longing.py && \
uv run python test_escape_valve.py && uv run python test_feedback.py && \
uv run python test_trigger.py && uv run python test_topics.py && \
uv run python test_circadian.py && uv run python test_followup.py   # full suite
```

注释 `# full suite (217 tests)` 改为不写死数字或写新计数。

- [ ] **Step 3: 全量回归**

Run: `cd /root/character_test && uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && uv run python test_integration.py && uv run python test_monitor.py && uv run python test_eventbus.py && uv run python test_personality.py && uv run python test_bayesian.py && uv run python test_composer.py && uv run python test_ebbinghaus.py && uv run python test_longing.py && uv run python test_escape_valve.py && uv run python test_feedback.py && uv run python test_trigger.py && uv run python test_topics.py && uv run python test_circadian.py && uv run python test_followup.py`
Expected: 16 个 runner 全部 PASS,退出码 0

- [ ] **Step 4: MEMORY.md 记录**

`MEMORY.md` 顶部新增条目(按现有格式,日期 2026-07-31,列出修改文件与功能描述:生物钟学习/接话茬/STATE_VERSION 7/测试新增 2 文件)。

- [ ] **Step 5: 自我审计(项目铁律)**

按 AGENTS.md「Before fixing: make a plan/todolist, dispatch parallel subagents, and self-audit with subagents after finishing」:派 1-2 个子代理交叉审计 chiguo_circadian.py / pending_topics / follow_up 触发逻辑(检查:时间戳 tz 处理、状态膨胀、与现有触发竞争、daemon 决策链路一致性),修掉发现的问题后重跑 Step 3。

---

## 自审检查

**1. Spec 覆盖:**
- §三 生物钟学习 → Task 1(纯函数)+ Task 2(集成)+ Task 5(toml)
- §四 接话茬 → Task 3(状态)+ Task 4(触发)+ Task 5(daemon/toml)+ Task 6(SKILL.md)
- §五 错误处理 → 冷启动回退(Task 2 测试)、topic 缺失兜底(Task 4)、memory_bridge 不可用(Task 4 守卫)、膨胀上限(Task 3)、旧 state 迁移(Task 2/3 测试)
- §六 测试计划 → 16 文件全量(Task 7)

**2. 占位符扫描:** 无 TBD/TODO,所有代码完整给出。

**3. 类型一致性:**
- `estimate_sleep_window(hour_counts, sample_days, history_days, min_sample_days, min_width, max_width)` — Task 1 定义与 Task 2 调用一致(关键字传参)
- `CircadianTracker(reply_days, quiet_start, quiet_end, confidence, sample_days)` — Task 1 定义、Task 2 加载(字段过滤)、Task 3 无涉及,一致
- `add_pending_topic(topic, now, source)` / `resolve_pending_topic(topic, now)` / `mark_pending_topic_attempted(topic)` / `prune_pending_topics(now, max_age_hours)` — Task 3 定义、Task 4 调用一致
- `follow_up` 触发 data 字段 `{topic, source, age_hours}` — Task 4 产生、Task 5 消费一致
- `_sync_quiet_window()` — Task 2 定义、Task 5 daemon 热重载调用一致

**4. 已知取舍(实现时遵守):**
- `estimate_sleep_window` 窗口语义 `quiet_end` 不含 end,与 `CooldownState._sleep_hours_in_range` 的 `qe < qs` 跨午夜约定一致
- 记忆兜底话题不落盘、不标记 attempted(评估失败下次可再试)
- follow_up 触发在 `evaluate_triggers` 内直接改 state(标记 attempted),daemon 两路径都会 `state.save()`,持久化无遗漏
- `prune_pending_topics` 仅在 evaluate_triggers(发送路径)调用,idle 路径话题保留(上限 20 防膨胀,无害)
