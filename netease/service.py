#!/usr/bin/env python3
# ============================================================
# netease/service.py — 网易云策略层:健康状态/登录失效检测/降级链/
#                      共享日配额/随机选源/音乐话题素材组装/播放反证单入口
# 依赖 netease.bridge(数据面,DI 注入),不依赖 chiguo_daemon。
# 零 LLM:输出结构化话题 dict,由 pi-agent 生成台词。
# 运行时文件锚定 <base_dir>/netease/。
# ============================================================

import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from netease.bridge import NeteaseBridge

CST = timezone(timedelta(hours=8))
DEFAULT_HEALTH_FILE = "netease_health.json"
HEALTH_SCHEMA_KEYS = ("api_alive", "logged_in", "faulty", "last_check",
                      "last_failure", "failure_reason",
                      "quota_music_day", "quota_music_used",
                      "quota_fault_day", "quota_fault_used")


class NeteaseService:
    """网易云策略层。构造注入 [netease]+[topic_picker] 配置、base_dir 与（可选）bridge。"""

    @staticmethod
    def _cfg_int(raw, default):
        """v9 审计 F-1:toml 数值兜底——非法/缺失/None → 默认;合法负值钳制为 0。
        热重载与构造共用,非法数值不再抛 ValueError 崩溃。"""
        try:
            return max(0, int(raw))
        except ValueError, TypeError:
            return default

    @staticmethod
    def _cfg_float(raw, default):
        try:
            return max(0.0, float(raw))
        except ValueError, TypeError:
            return default

    def __init__(self, config: dict, base_dir: str,
                 bridge: NeteaseBridge | None = None):
        net = config.get("netease", {}) or {}
        tp = config.get("topic_picker", {}) or {}
        self.retry_count = self._cfg_int(net.get("retry_count", 1), 1)
        self.retry_backoff = self._cfg_float(net.get("retry_backoff_seconds", 2.0), 2.0)
        self.reprobe_minutes = self._cfg_float(net.get("reprobe_minutes", 30.0), 30.0)
        self.daily_quota = self._cfg_int(tp.get("netease_daily_quota", 2), 2)
        self.fault_quota = self._cfg_int(tp.get("netease_fault_daily_quota", 1), 1)
        self.source_weights = [0.5, 0.5]
        sw = tp.get("netease_source_weights")
        if isinstance(sw, list) and len(sw) == 2:
            try:
                w0, w1 = max(0.0, float(sw[0])), max(0.0, float(sw[1]))  # 负权重钳制为 0
                if w0 > 0 or w1 > 0:  # 两权重全 ≤0 → 回退默认(退化分布防御)
                    self.source_weights = [w0, w1]
            except TypeError, ValueError:
                pass
        self.enabled = bool(net.get("enabled", True))   # 可选来源开关
        self.play_cache_ttl_minutes = self._cfg_int(net.get("play_cache_ttl_minutes", 15), 15)
        self.base_dir = Path(base_dir)
        self.data_dir = self.base_dir / "netease"
        self.health_file = self.data_dir / DEFAULT_HEALTH_FILE
        self.bridge = bridge or NeteaseBridge(
            base_dir=base_dir,
            retry_count=self.retry_count,
            retry_backoff=self.retry_backoff,
        )
        self._health = self._load_health()

    # ── 健康文件 ──
    def _default_health(self) -> dict:
        today = datetime.now(CST).strftime("%Y-%m-%d")
        return {
            "api_alive": None, "logged_in": None, "faulty": False,
            "last_check": None, "last_failure": None, "failure_reason": None,
            "quota_music_day": today, "quota_music_used": 0,
            "quota_fault_day": today, "quota_fault_used": 0,
        }

    def _load_health(self) -> dict:
        """损坏/缺失/结构不符/值类型非法 → 回退默认(不崩溃)。
        配额数值字段须为 int 且 ≥0(排除 bool——bool 是 int 子类);
        布尔字段须为 bool;非法值丢弃回默认。"""
        try:
            with open(self.health_file) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._default_health()
            merged = self._default_health()
            for k, v in data.items():
                if k not in HEALTH_SCHEMA_KEYS:
                    continue  # 未知键丢弃
                if k in ("quota_music_used", "quota_fault_used"):
                    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                        merged[k] = v
                elif k in ("api_alive", "logged_in", "faulty"):
                    if isinstance(v, bool):
                        merged[k] = v
                else:
                    merged[k] = v
            return merged
        except Exception:
            return self._default_health()

    def _save_health(self):
        """原子写 .tmp → os.replace;失败仅 warn。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = f"{self.health_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._health, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.health_file)
        except Exception as e:
            print(f"[warn] netease_health 写入失败: {e}", file=sys.stderr)

    def health(self) -> dict:
        """给 monitor 的快照副本(浅拷贝,修改不影响内部状态)。"""
        return dict(self._health)

    # ── 配额 ──
    def _roll_quota(self, now: datetime):
        today = now.strftime("%Y-%m-%d")
        if self._health.get("quota_music_day") != today:
            self._health["quota_music_day"] = today
            self._health["quota_music_used"] = 0
        if self._health.get("quota_fault_day") != today:
            self._health["quota_fault_day"] = today
            self._health["quota_fault_used"] = 0

    def _music_quota_left(self, now) -> int:
        self._roll_quota(now)
        return max(0, self.daily_quota - self._health["quota_music_used"])

    def _fault_quota_left(self, now) -> int:
        self._roll_quota(now)
        return max(0, self.fault_quota - self._health["quota_fault_used"])

    def _consume_music(self, now):
        self._roll_quota(now)
        self._health["quota_music_used"] = min(self.daily_quota,
                                                self._health["quota_music_used"] + 1)
        self._save_health()

    def _consume_fault(self, now):
        self._roll_quota(now)
        self._health["quota_fault_used"] = min(self.fault_quota,
                                                 self._health["quota_fault_used"] + 1)
        self._save_health()

    # ── 健康刷新(单一入口:判定 faulty 与原因) ──
    def refresh_health(self, now: datetime | None = None) -> dict:
        """真实探针:check_health()(不走缓存)。api_alive=False → faulty=unreachable;
        api_alive 且 logged_in=False → faulty=login_expired;均 OK → faulty=None(恢复)。"""
        now = now or datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        h = self.bridge.check_health()
        self._health["last_check"] = now.isoformat()
        if h is None or not h.get("api_alive"):
            self._health["api_alive"] = False
            self._set_faulty("unreachable", now)
        elif not h.get("logged_in"):
            self._health["api_alive"] = True
            self._health["logged_in"] = False
            # 301=需重新登录（login_expired）；其余 code!=200 才是 api_error（API 异常 ≠ 登录失效）
            reason = "login_expired" if h.get("api_error") in (None, 301) else "api_error"
            self._set_faulty(reason, now)
        else:
            self._health["api_alive"] = True
            self._health["logged_in"] = True
            self._set_faulty(None, now)
        self._save_health()
        return self.health()

    def _set_faulty(self, reason: str | None, now: datetime):
        if reason is None:
            self._health["faulty"] = False
            self._health["last_failure"] = None
            self._health["failure_reason"] = None
        else:
            newly = not self._health.get("faulty")
            self._health["faulty"] = True
            self._health["failure_reason"] = reason
            if newly:  # 只在转为故障时记录首次失败时刻
                self._health["last_failure"] = now.isoformat()

    def _should_reprobe(self, now: datetime) -> bool:
        last = self._health.get("last_check")
        if not last:
            return True
        try:
            lc = datetime.fromisoformat(last)
        except ValueError, TypeError:
            return True
        if lc.tzinfo is None:
            lc = lc.replace(tzinfo=CST)
        return (now - lc).total_seconds() / 60 >= self.reprobe_minutes

    # ── 播放反证单入口(daemon 专用) ──
    def fetch_play_proof(self, now: datetime) -> list | None:
        """daemon 播放反证专用:包装 bridge.fetch_recent_play。
        enabled=False → None;naive now 补 CST;拉取后按 _should_reprobe 门控刷新
        health(keep last_check fresh,不放大 API 调用频率)。
        缓存锚定 <data_dir>/recent_play_cache.json。"""
        if not self.enabled:
            return None
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        plays = self.bridge.fetch_recent_play(
            limit=20, ttl_minutes=self.play_cache_ttl_minutes, now=now)
        if self._should_reprobe(now):
            self.refresh_health(now)
        return plays

    # ── 话题入口(两阶段:peek 探测不消费 / consume 选中后确认) ──

    def peek_music_topic(self, now: datetime, in_class: bool = False,
                         in_quiet_window: bool = False) -> dict | None:
        """探测候选话题,但不消费配额。
        供 TopicPicker 在加权抽选前探测候选;选中后须调 consume_music_topic/consume_fault_topic
        确认消费。拉取成功仍同步健康恢复(_sync_success 只改健康态不消费配额);
        两源全失败仍走 refresh_health 探针。
        时段门禁优先于故障分支:上课/睡眠窗口恒 None(故障话题也受时段约束,R13)。
        enabled=False → 直接 None(不拉取不消费,与 fetch_play_proof 一致,A3)。"""
        if not self.enabled:
            return None
        now = now or datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        # 时段门禁前置:上课/睡眠窗口连故障话题也不发(与正常话题同级静默,R13)
        if in_class or in_quiet_window:
            return None
        if self._health.get("faulty"):
            if self._should_reprobe(now):
                self.refresh_health(now)
            if self._health.get("faulty"):
                if self._fault_quota_left(now) <= 0:
                    return None
                return {
                    "type": "netease_fault",
                    "hint": "网易云好像不理我了，跟哥哥念叨一句音乐服务不太给力",
                    "tone": "playful",
                    "data": {"source": "fault", "reason": self._health.get("failure_reason")},
                }
            # 恢复 → 继续走正常话题
        if self._music_quota_left(now) <= 0:
            return None
        return self._pick_and_fetch(now, consume=False)

    def consume_music_topic(self, now):
        """选中后确认消费(音乐配额+1)。健康恢复已在拉取成功时由 _sync_success 完成。"""
        now = now or datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        self._consume_music(now)

    def consume_fault_topic(self, now):
        """选中后确认消费(故障配额+1)。"""
        now = now or datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        self._consume_fault(now)

    def _pick_and_fetch(self, now: datetime, consume: bool = True) -> dict | None:
        """加权随机选源;选中源不可用 → 自动换另一个;都不可用 → None(不消费配额)。
        consume=False(peek 路径):跳过 _consume_music,但拉取成功仍 _sync_success
        恢复健康(拉取成功=API 正常是事实,且该操作不消费配额)。
        两源全失败后调用 refresh_health(now) 真实探针判定健康态:
        API 宕机/登录失效 → 置 faulty(下一次 peek 走故障话题分支,受 fault 配额约束);
        探针 healthy(如每日推荐为空但 API 正常)→ 保持静默回退(数据空不是故障)。"""
        total = self.source_weights[0] + self.source_weights[1]
        r = random.random() * total
        first = "daily" if r < self.source_weights[0] else "recent"
        second = "recent" if first == "daily" else "daily"
        for source in (first, second):
            topic = self._fetch_source_topic(source, now)
            if topic:
                self._sync_success(now)
                if consume:
                    self._consume_music(now)
                return topic
        self.refresh_health(now)  # 两源全失败 → 真实探针判定故障态(本轮仍 None,不消费配额)
        return None

    def _fetch_source_topic(self, source: str, now: datetime) -> dict | None:
        if source == "daily":
            songs = self.bridge.fetch_daily_songs(limit=10)
            if not songs:
                return None
            song = random.choice(songs)
            return {
                "type": "netease_music",
                "hint": f"今天网易云给哥哥推荐了《{song['name']}》，问哥哥听过没有",
                "tone": "casual",
                "data": {"source": "daily", "name": song["name"], "artist": song["artists"]},
            }
        plays = self.bridge.fetch_recent_play(limit=20, ttl_minutes=self.play_cache_ttl_minutes, now=now)
        if not plays:
            return None
        newest = max(plays, key=lambda p: p.get("playTime", 0))
        return {
            "type": "netease_music",
            "hint": f"哥哥最近在听《{newest['name']}》，我想跟着听听看",
            "tone": "casual",
            "data": {"source": "recent", "name": newest["name"], "artist": newest["artist"]},
        }

    def _sync_success(self, now: datetime):
        """拉取成功 → 标记恢复(即使之前 faulty)。"""
        if self._health.get("faulty"):
            self._set_faulty(None, now)
            self._save_health()
