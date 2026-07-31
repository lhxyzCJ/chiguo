# 网易云音乐渠道增强实施计划 — 对话内容源 + 鲁棒性

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 网易云音乐接入对话内容(topic_picker 第 8 源:每日推荐/播放历史共享配额随机选源,故障角色化提及)+ 桥接层鲁棒性加固(有限重试/schema 校验统一化/登录失效检测/降级链/monitor 集成)。

**Architecture:** 分层——`netease_bridge.py` 保持纯数据面(HTTP/解析/缓存,新增重试与 schema 过滤),新增 `chiguo_netease.py` 策略层 `NeteaseService`(健康状态持久化 netease_health.json、登录失效检测、降级链、共享日配额、随机选源、音乐话题素材组装);`chiguo_topics.py` 新增第 8 源委托策略层;`chiguo_daemon.py` 构造时注入 NeteaseService;`chiguo_monitor.py` health() 读健康文件。play-proof(睡眠窗口反证)不动,复用加固后的桥接层。

**Tech Stack:** Python 3.14 (uv), stdlib, 本地 NeteaseCloudMusicApi (localhost:3000, 不可用优雅降级)。

**Spec:** `docs/superpowers/specs/2026-07-31-netease-music-channel-design.md`(已批准)

**注意:项目不是 git 仓库**(AGENTS.md 确认)——所有「Commit」步骤替换为「Task 6 统一记录 MEMORY.md + 文档同步」。

---

### Task 1: 桥接层加固 — `_api_get` 有限重试 + `fetch_daily_songs` schema 过滤

**Files:**
- Modify: `/root/character_test/netease_bridge.py`
- Modify: `/root/character_test/test_netease_proof.py`

**接口契约(精确):**

```python
# 模块级重试策略(测试/策略层注入;默认 1 次 / 2.0 秒)
_API_RETRY_COUNT = 1
_API_RETRY_BACKOFF = 2.0

def set_api_retry_policy(retry_count: int, backoff_seconds: float) -> None:
    """策略层(NeteaseService)启动时注入重试参数;越界值钳制为非负。"""

def _api_get(path, cookie=None, timeout=10):
    """瞬时失败(URLError 非 HTTPError / HTTPError code>=500)→ 重试 _API_RETRY_COUNT 次,
    间隔 _API_RETRY_BACKOFF 秒;非瞬时(HTTPError 4xx / JSON 解析 / 其他)→ 直接返回 None。
    全部重试耗尽 → 打印 error 并返回 None。"""
```

**重试语义**(`_api_get` 内部,替换现有单一 try/except):
- 捕获顺序:先 `except urllib.error.HTTPError as e`(HTTPError 是 URLError 子类,必须在 URLError 之前):`e.code >= 500` 且重试次数未耗尽 → sleep + continue;否则打印 `[error] API HTTP {e.code}: {e.reason}` → return None
- 再 `except urllib.error.URLError as e`:重试次数未耗尽 → sleep + continue;耗尽 → 打印 error → return None
- `except json.JSONDecodeError` / `except Exception`:不重试,直接 return None(现有行为)
- 重试耗尽后打印 `[error] API unreachable after N attempts: {last_err}`

**`fetch_daily_songs` schema 逐条过滤**(替换现有 `for s in raw_songs[:limit]` 循环):

```python
    for s in raw_songs[:limit]:
        if not isinstance(s, dict):
            continue  # 非 dict 条目 → 过滤
        raw_artists = s.get("ar")
        raw_artists = raw_artists if isinstance(raw_artists, list) else []
        artists = [ar.get("name", "") for ar in raw_artists if isinstance(ar, dict)]
        artist_str = "/".join(a for a in artists if a) if artists else "未知"
        album = s.get("al")
        album = album if isinstance(album, dict) else {}
        song_id = s.get("id")
        if not isinstance(song_id, int):
            continue  # 无合法 id → 过滤(share_url 依赖它)
        song = {
            "id": song_id,
            "name": s.get("name", ""),
            "artists": artist_str,
            "album": album.get("name", ""),
            "pic_url": album.get("picUrl", ""),
            "dt_ms": s.get("dt", 0) if isinstance(s.get("dt"), int) else 0,
            "fee": s.get("fee", 0) if isinstance(s.get("fee"), int) else 0,
            "share_url": f"https://music.163.com/song?id={song_id}",
        }
        songs.append(song)
```

**测试断言清单**(`test_netease_proof.py` 追加,monkeypatch `_api_get` 依赖的 `urllib.request.urlopen`):

```python
def test_api_get_retries_transient_failure():   # 第一次 urlopen 抛 URLError → 第二次成功 → 返回 JSON
def test_api_get_no_retry_on_http_4xx():        # HTTPError(403) → 立即 None,urlopen 只调 1 次
def test_api_get_retries_http_5xx():            # HTTPError(503) → 重试后成功
def test_api_get_retry_policy_zero():           # set_api_retry_policy(0, 0) → 不重试;用完恢复默认
def test_daily_songs_schema_filter():           # raw_songs 混合合法/非 dict/ar 非 list/id 缺失 → 仅保留合法条目
```

- [ ] **Step 1: 写失败测试** — 在 `test_netease_proof.py` 末尾追加上述 5 个测试(参考现有 `_fake_ok`/monkeypatch 模式:用 `urllib.request.urlopen` 被 patch 的 fake,返回 `fake_response` 对象需有 `read()`;HTTPError 用 `urllib.error.HTTPError(url, code, msg, hdrs, fp)` 构造或自定义异常)
- [ ] **Step 2: 运行确认失败** — `uv run python test_netease_proof.py`,预期 retry 相关测试失败(无重试逻辑)
- [ ] **Step 3: 实现 `set_api_retry_policy` + 重写 `_api_get`**(见上面契约;注意 HTTPError 捕获在 URLError 之前)
- [ ] **Step 4: 实现 `fetch_daily_songs` 循环替换**(见上面代码,保持其余函数不变)
- [ ] **Step 5: 运行确认通过** — `uv run python test_netease_proof.py`,预期全部 PASS(含既有 24 用例)

---

### Task 2: 策略层 `chiguo_netease.py`(新文件)+ `test_netease_service.py`(新文件)

**Files:**
- Create: `/root/character_test/chiguo_netease.py`
- Create: `/root/character_test/test_netease_service.py`

**`chiguo_netease.py` 完整结构:**

```python
#!/usr/bin/env python3
# ============================================================
# chiguo_netease.py — 网易云策略层:健康状态/登录失效检测/降级链/
#                     共享日配额/随机选源/音乐话题素材组装
# 依赖 netease_bridge(数据面),不依赖 chiguo_daemon。
# 零 LLM:输出结构化话题 dict,由 OpenClaw 生成台词。
# ============================================================

import json
import os
import random
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import netease_bridge

CST = timezone(timedelta(hours=8))
DEFAULT_HEALTH_FILE = "netease_health.json"
HEALTH_SCHEMA_KEYS = ("api_alive", "logged_in", "faulty", "last_check",
                      "last_failure", "failure_reason",
                      "quota", "quota_music_day", "quota_music_used",
                      "quota_fault_day", "quota_fault_used")


def _in_quiet_window(dt: datetime, start: int, end: int) -> bool:
    """与 chiguo_daemon._in_quiet_window 同语义(跨午夜)。"""
    if end < start:
        return dt.hour >= start or dt.hour < end
    return start <= dt.hour < end


class NeteaseService:
    """网易云策略层。构造注入 [netease]+[topic_picker] 配置与 base_dir(健康文件锚定)。"""

    def __init__(self, config: dict, base_dir: str):
        net = config.get("netease", {}) or {}
        tp = config.get("topic_picker", {}) or {}
        self.retry_count = max(0, int(net.get("retry_count", 1)))
        self.retry_backoff = max(0.0, float(net.get("retry_backoff_seconds", 2.0)))
        self.reprobe_minutes = max(0.0, float(net.get("reprobe_minutes", 30.0)))
        self.daily_quota = max(0, int(tp.get("netease_daily_quota", 2)))
        self.fault_quota = max(0, int(tp.get("netease_fault_daily_quota", 1)))
        self.source_weights = [0.5, 0.5]
        sw = tp.get("netease_source_weights")
        if isinstance(sw, list) and len(sw) == 2:
            try:
                w0, w1 = float(sw[0]), float(sw[1])
                if w0 > 0 or w1 > 0:
                    self.source_weights = [w0, w1]
            except (TypeError, ValueError):
                pass
        self.base_dir = Path(base_dir)
        self.health_file = self.base_dir / DEFAULT_HEALTH_FILE
        netease_bridge.set_api_retry_policy(self.retry_count, self.retry_backoff)
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
        """损坏/缺失/结构不符 → 默认重建(不崩溃)。"""
        try:
            with open(self.health_file) as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return self._default_health()
            merged = self._default_health()
            merged.update({k: v for k, v in data.items() if k in HEALTH_SCHEMA_KEYS})
            return merged
        except Exception:
            return self._default_health()

    def _save_health(self):
        """原子写 .tmp → os.replace;失败仅 warn。"""
        tmp = f"{self.health_file}.tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(self._health, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.health_file)
        except Exception as e:
            print(f"[warn] netease_health 写入失败: {e}", file=sys.stderr)

    def health(self) -> dict:
        """给 monitor 的快照(引用,只读)。"""
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
        self._health["quota_music_used"] += 1
        self._save_health()

    def _consume_fault(self, now):
        self._roll_quota(now)
        self._health["quota_fault_used"] += 1
        self._save_health()

    # ── 健康刷新(单一入口:判定 faulty 与原因) ──
    def refresh_health(self, now: datetime | None = None) -> dict:
        """真实探针:check_health()(不走缓存)。api_alive=False → faulty=unreachable;
        api_alive 且 logged_in=False → faulty=login_expired;均 OK → faulty=None(恢复)。"""
        now = now or datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        h = netease_bridge.check_health()
        self._health["last_check"] = now.isoformat()
        if h is None or not h.get("api_alive"):
            self._health["api_alive"] = False
            self._set_faulty("unreachable", now)
        elif not h.get("logged_in"):
            self._health["api_alive"] = True
            self._health["logged_in"] = False
            self._set_faulty("login_expired", now)
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
        except (ValueError, TypeError):
            return True
        if lc.tzinfo is None:
            lc = lc.replace(tzinfo=CST)
        return (now - lc).total_seconds() / 60 >= self.reprobe_minutes

    # ── 话题入口 ──
    def music_topic(self, now: datetime, in_class: bool = False,
                    in_quiet_window: bool = False) -> dict | None:
        """正常音乐话题:时段门控(上课/睡眠)→None + 配额 + 随机选源。
        故障话题:不受时段门控,受 fault 配额;faulty 且未到重探间隔 → 快速跳过网络请求。"""
        now = now or datetime.now(CST)
        if now.tzinfo is None:
            now = now.replace(tzinfo=CST)
        if self._health.get("faulty"):
            if self._should_reprobe(now):
                self.refresh_health(now)
            if self._health.get("faulty"):
                return self._fault_topic(now)
            # 恢复 → 继续走正常话题
        if in_class or in_quiet_window:
            return None
        if self._music_quota_left(now) <= 0:
            return None
        return self._pick_and_fetch(now)

    def _fault_topic(self, now: datetime) -> dict | None:
        if self._fault_quota_left(now) <= 0:
            return None
        self._consume_fault(now)
        return {
            "type": "netease_fault",
            "hint": "网易云好像不理菓菓了，跟哥哥念叨一句音乐服务不太给力",
            "tone": "playful",
            "data": {"source": "fault", "reason": self._health.get("failure_reason")},
        }

    def _pick_and_fetch(self, now: datetime) -> dict | None:
        """加权随机选源;选中源不可用 → 自动换另一个;都不可用 → None(不消费配额)。"""
        order = ["daily", "recent"]
        total = self.source_weights[0] + self.source_weights[1]
        r = random.random() * total
        first = "daily" if r < self.source_weights[0] else "recent"
        second = "recent" if first == "daily" else "daily"
        for source in (first, second):
            topic = self._fetch_source_topic(source, now)
            if topic:
                self._sync_success(now)
                self._consume_music(now)
                return topic
        return None

    def _fetch_source_topic(self, source: str, now: datetime) -> dict | None:
        if source == "daily":
            songs = netease_bridge.fetch_daily_songs(limit=10)
            if not songs:
                return None
            song = random.choice(songs)
            return {
                "type": "netease_music",
                "hint": f"今天网易云给哥哥推荐了《{song['name']}》，问哥哥听过没有",
                "tone": "casual",
                "data": {"source": "daily", "name": song["name"], "artist": song["artists"]},
            }
        plays = netease_bridge.fetch_recent_play(
            limit=20, ttl_minutes=int(self._ttl_minutes()), now=now)
        if not plays:
            return None
        newest = max(plays, key=lambda p: p.get("playTime", 0))
        return {
            "type": "netease_music",
            "hint": f"哥哥最近在听《{newest['name']}》，菓菓想跟着听听看",
            "tone": "casual",
            "data": {"source": "recent", "name": newest["name"], "artist": newest["artist"]},
        }

    def _ttl_minutes(self) -> float:
        try:
            return max(1.0, float(netease_bridge.fetch_recent_play.__defaults__[0]))
        except Exception:
            return 15.0

    def _sync_success(self, now: datetime):
        """拉取成功 → 标记恢复(即使之前 faulty)。"""
        if self._health.get("faulty"):
            self._set_faulty(None, now)
            self._save_health()
```

> 说明:健康状态刷新时机 = ①`music_topic` 遇到 faulty 且到重探间隔;②每次成功拉取 `_sync_success`(恢复);③monitor 侧只读文件,不触发探针。fetch 失败时 `music_topic` 返回 None 且不消费配额——降级链在桥接层(新鲜缓存→过期缓存→None)。

**测试断言清单**(`test_netease_service.py`,新 runner,参照 `test_netease_proof.py` 模式:monkeypatch `netease_bridge.fetch_daily_songs`/`fetch_recent_play`/`check_health`;固定 CST 时间;`random.seed` 固定;临时目录做 base_dir;结束恢复 bridge 模块状态):

1. `test_health_file_default_when_missing`:无文件 → `_default_health` 结构,不崩溃
2. `test_health_file_corrupt_rebuild`:写垃圾内容 → 加载重建默认
3. `test_health_file_atomic_write`:save 后文件存在且内容合法;quota 字段持久化
4. `test_quota_shared_across_sources`:配额 2,seed 固定,连续 3 次 music_topic(两源都可用)→ 前 2 次非 None、第 3 次 None;`quota_music_used`==2
5. `test_quota_rolls_over_day`:now 跨天 → 配额重置
6. `test_random_source_selection`:seed 固定、两个源都可用、配额 20 → 抽样 200 次,type 中 daily 与 recent 均出现(证明随机选源而非固定)
7. `test_source_fallback_when_daily_down`:fetch_daily_songs → None、fetch_recent_play → 1 条 → 产出 recent 话题且消费配额 1
8. `test_both_sources_down_no_quota`:两源都 None → 返回 None,quota_music_used 不变
9. `test_time_gate_in_class`:in_class=True → None
10. `test_time_gate_quiet_window`:in_quiet_window=True → None
11. `test_fault_topic_bypasses_time_gate`:refresh_health 置 faulty → in_class=True 且 fault 配额内 → 产出 netease_fault 话题
12. `test_fault_quota`:fault 配额 1 → 第 2 次调用(仍 faulty)→ None
13. `test_login_expired_detection`:check_health → {"api_alive": True, "logged_in": False} → refresh_health 后 faulty=True、reason=login_expired
14. `test_faulty_fast_skip_until_reprobe`:faulty 且 last_check 刚更新 → music_topic 不调用 fetch(check_health 不被调),直接产出故障话题/None
15. `test_faulty_reprobe_after_interval`:last_check 早于 reprobe_minutes → refresh_health 被调用
16. `test_recovery_after_success`:faulty 状态 → _pick_and_fetch 成功 → health.faulty=False、last_failure=None
17. `test_fault_topic_no_link_in_data`:fault/daily/recent 话题的 data 均不含 share_url/链接字段
18. `test_recent_uses_newest_play`:plays 乱序(playTime 不同)→ 素材取 playTime 最大者
19. `test_music_topic_naive_now`:naive now → 补齐 CST 不崩
20. `test_source_weights_from_toml`:构造 cfg 带 netease_source_weights=[1,0] → 只选 daily

- [ ] **Step 1: 写失败测试** — `test_netease_service.py` 实现上述 20 用例(monkeypatch 三函数;每个测试用 `tempfile.TemporaryDirectory`;结尾恢复 bridge 原函数;`NeteaseService` 尚未实现 → 导入失败)
- [ ] **Step 2: 运行确认失败** — `uv run python test_netease_service.py`,预期 ImportError/AttributeError
- [ ] **Step 3: 写 `chiguo_netease.py`**(上面完整代码;`_ttl_minutes` 若 `__defaults__` 取不到 ttl 直接硬编码 15.0,测试不依赖它)
- [ ] **Step 4: 运行确认通过** — `uv run python test_netease_service.py`,预期全部 PASS

---

### Task 3: toml 配置 + TopicPicker 第 8 源接入

**Files:**
- Modify: `/root/character_test/chiguo_proactive.toml`
- Modify: `/root/character_test/chiguo_topics.py`
- Modify: `/root/character_test/test_topics.py`

**toml 变更:**

```toml
[netease]
play_cache_ttl_minutes = 15          # 现有(v8)
play_proof_window_hours = 2.0        # 现有(v8)
sleeping_confidence_factor = 0.5     # 现有(v8)
retry_count = 1                      # v9 新增:瞬时失败重试次数
retry_backoff_seconds = 2.0          # v9 新增:重试退避(秒)
reprobe_minutes = 30                 # v9 新增:登录失效后重探间隔(分钟)

[topic_picker]
netease_weight = 0.12                # v9 新增:音乐话题源权重
netease_daily_quota = 2              # v9 新增:音乐话题日配额(每日推荐+播放历史共享)
netease_source_weights = [0.5, 0.5]  # v9 新增:每日推荐 vs 播放历史 随机选源权重
netease_fault_daily_quota = 1        # v9 新增:故障提及日配额
```

**`chiguo_topics.py` 变更:**

```python
# 文件头 docstring:7 个来源 → 8 个来源(新增 netease)

class TopicPicker:
    def __init__(self, state, config: dict, netease_service=None):
        self.state = state
        self.netease_service = netease_service  # v9: 策略层,可为 None(降级)
        self.weights = {
            "schedule": config.get("schedule_weight", 0.30),
            "memory": config.get("memory_weight", 0.25),
            "weather_season": config.get("weather_season_weight", 0.20),
            "general": config.get("general_weight", 0.25),
            "solar_terms": config.get("solar_terms_weight", 0.10),
            "anniversary": config.get("anniversary_weight", 0.15),
            "preference_followup": config.get("preference_followup_weight", 0.10),
            "netease": config.get("netease_weight", 0.12),  # v9
        }
        ...

    def pick(self, now: datetime) -> dict | None:
        ...  # 现有逻辑中,在 pref 之后插入:
        netease = self._netease_music_topic(now)
        if netease:
            candidates.append({"topic": netease, "weight": weights["netease"]})
        ...

    # ── 来源 8:网易云音乐(策略层委托) ──
    def _netease_music_topic(self, now: datetime) -> dict | None:
        """v9: 委托 NeteaseService.music_topic。未注入 → None(不崩溃)。
        时段门控:上课/睡眠由本方法计算(schedule_status + cooldown.quiet_window)。"""
        if not self.netease_service:
            return None
        try:
            in_class = False
            sch = self.state.schedule_status(now)
            in_class = bool(sch and sch.get("in_class"))
        except Exception:
            in_class = False
        try:
            qs, qe = self.state.cooldown.quiet_window()
            in_quiet = _in_quiet_window(now, int(qs), int(qe))
        except Exception:
            in_quiet = False
        try:
            return self.netease_service.music_topic(now, in_class=in_class,
                                                    in_quiet_window=in_quiet)
        except Exception:
            return None  # 策略层异常 → 静默跳过(不阻塞话题选择)
```

模块级 `_in_quiet_window` 从 `chiguo_netease` 导入,避免复制:

```python
from chiguo_netease import NeteaseService, _in_quiet_window  # 顶部导入
```

**测试断言清单**(`test_topics.py` 追加):

1. `test_netease_weight_in_weights`:构造后 `picker.weights["netease"]` == 0.12(真实 toml)
2. `test_netease_topic_emitted_when_service_injected`:注入 fake `NeteaseService`(SimpleNamespace 提供 `music_topic` 返回固定话题 dict,记录调用参数)→ 种子序列中 `pick` 可出现 type=="netease_music"(配额不设限,seed 扫 300)
3. `test_netease_service_called_with_gate_args`:fake service 记录 `music_topic(now, in_class, in_quiet_window)` 参数 → 断言 in_class/in_quiet_window 与 mock state 的 schedule_status/quiet_window 一致
4. `test_netease_not_injected_no_crash`:不注入 → pick 恒不产出 netease 类型,不抛异常
5. `test_netease_service_exception_silent`:fake service 的 music_topic 抛异常 → pick 不崩
6. `test_pick_valid_set_includes_netease`:注入 fake service(恒返回 netease 话题)+ 真实 state,300 种子扫 pick → 结果类型落在含 `"netease_music"`/`"netease_fault"` 的合法集合内(本测试独立构造 valid 集合,不改现有 91 行测试——现有测试均不注入 service)

- [ ] **Step 1: 更新 toml**(新增 5 行 [netease] 键 + 4 行 [topic_picker] 键)
- [ ] **Step 2: 改 `chiguo_topics.py`**(顶部 import + weights 加键 + pick 插入 + `_netease_music_topic` 方法)
- [ ] **Step 3: 写/更新测试**(上述 6 个;MockState 加 `cooldown.quiet_window` 方法返回 (0,8))
- [ ] **Step 4: 运行** — `uv run python test_topics.py`,预期全部 PASS(既有 14 用例 + 新增)

---

### Task 4: daemon 注入 NeteaseService

**Files:**
- Modify: `/root/character_test/chiguo_daemon.py`

**变更(精确):**

```python
# 顶部导入区(现有 import 之后):
from chiguo_netease import NeteaseService

# __init__ 中,现有第 71 行替换:
        self.state = ChiguoState(self.config)
        # v9: 网易云策略层(健康/配额/音乐话题),base_dir 锚定
        self.netease_service = NeteaseService(self.config, str(self._base_dir))
        self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                        netease_service=self.netease_service)

# _maybe_reload_config 中,现有第 117 行替换:
            # v9: 热重载时同步重建策略层(重试/配额参数可能被改)与 TopicPicker
            self.netease_service = NeteaseService(self.config, str(self._base_dir))
            self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                            netease_service=self.netease_service)
```

> 注:热重载分支与 `__init__` 均重建 `NeteaseService`(上面代码已覆盖两处),保持重试/配额参数与 toml 同步。

**验证**:`uv run python chiguo_daemon.py --status`(只读,不写生产状态)输出合法 JSON 不崩;`uv run python -c "import chiguo_daemon"` 不报导入错误。

- [ ] **Step 1: 实现变更**(推荐在热重载分支也重建 NeteaseService)
- [ ] **Step 2: 验证导入** — `uv run python -c "from chiguo_daemon import DecisionEngine; print('ok')"`
- [ ] **Step 3: 冒烟** — `uv run python chiguo_daemon.py --status` 正常输出(仅当 NeteaseCloudMusicApi 在跑;不可用也不崩)

---

### Task 5: monitor health() 网易云项

**Files:**
- Modify: `/root/character_test/chiguo_monitor.py`
- Modify: `/root/character_test/test_monitor.py`

**变更(精确):** 在 `health()` 的 issues 检测段(现 1-3 项之后)追加:

```python
        # 4. 网易云音乐桥健康(只读 netease_health.json;缺失/损坏 → 跳过不告警)
        nh_path = self.config_path.parent / "netease_health.json"
        try:
            with open(nh_path) as f:
                nh = json.load(f)
            if isinstance(nh, dict) and nh.get("faulty"):
                reason = nh.get("failure_reason") or "?"
                healthy = False
                issues.append(
                    f"netease music API faulty (reason={reason}, "
                    f"api_alive={nh.get('api_alive')}, logged_in={nh.get('logged_in')})")
        except (OSError, json.JSONDecodeError):
            pass  # 文件缺失/损坏 → 未知,不告警
```

**测试断言清单**(`test_monitor.py` 追加,构造 Monitor 指向临时目录,参考现有 test_monitor 的临时目录模式):

1. `test_health_netease_faulty`:临时目录写 config(toml 副本)+ `netease_health.json`(faulty=True, failure_reason="login_expired")→ `health()` 中 healthy=False 且 issues 含 netease 字样
2. `test_health_netease_healthy`:faulty=False 文件 → 不产生 netease issue
3. `test_health_netease_missing_file`:无健康文件 → 不崩、无 netease issue

- [ ] **Step 1: 实现 health() 变更**(确认 `json` 已在 chiguo_monitor.py 顶部导入;没有则加)
- [ ] **Step 2: 写测试**(复用 test_monitor.py 既有临时目录/构造方式;Monitor 构造参数 config_path 传临时目录内的 toml 副本)
- [ ] **Step 3: 运行** — `uv run python test_monitor.py`,预期全部 PASS(既有 39 用例 + 新增 3)

---

### Task 6: 全量回归 + 文档同步 + MEMORY.md

**Files:**
- Modify: `/root/character_test/doc/SYSTEM.md`
- Modify: `/root/character_test/doc/README.md`
- Modify: `/root/character_test/doc/IMPROVE.md`
- Modify: `/root/character_test/MEMORY.md`
- Modify: `/root/character_test/AGENTS.md`(17→18 文件 + 模块表 chiguo_netease)

**验证(全量,不允许无参数跑 daemon 写生产):**

```bash
cd /root/character_test
uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && \
uv run python test_integration.py && uv run python test_monitor.py && \
uv run python test_eventbus.py && uv run python test_personality.py && \
uv run python test_bayesian.py && uv run python test_composer.py && \
uv run python test_ebbinghaus.py && uv run python test_longing.py && \
uv run python test_escape_valve.py && uv run python test_feedback.py && \
uv run python test_trigger.py && uv run python test_topics.py && \
uv run python test_circadian.py && uv run python test_followup.py && \
uv run python test_netease_proof.py && uv run python test_netease_service.py
```

预期:18 文件全 PASS(预计 289 + 新增 ≈ 315+)。

**文档变更要点:**
- `doc/SYSTEM.md`:`netease_bridge` 描述补「v9: 有限重试/schema 过滤」;新增 `chiguo_netease.py` 模块表行;§2.11 补 v9 小节(策略层/配额/故障提及);配置表 + [netease] 新键 + [topic_picker] 新键;测试表 17→18 文件、289→新计数;版本历史 v9;§十一「topic_picker 未接入」状态改为「已接入(v9)」
- `doc/README.md`:架构图 + `chiguo_netease.py` 行;测试计数
- `doc/IMPROVE.md`:新增 v9 记录节(文件变更表 + 设计取舍 + 验证)
- `MEMORY.md`:新增顶部记录「2026-07-31 — v9 网易云音乐渠道增强」(日期、文件、描述、测试计数)
- `AGENTS.md`:测试命令链加 `test_netease_service.py`;架构快速表加 `chiguo_netease.py`

- [ ] **Step 1: 跑全量回归**,记录实际计数
- [ ] **Step 2: 文档同步**(上述 5 个文件;计数用实际值)
- [ ] **Step 3: 核对** — `grep -rn "289" doc/ MEMORY.md AGENTS.md` 确认旧计数全替换

---

### Task 7: 全面审计(用户明确要求)

**Files:**
- 只读:`/root/character_test/`(所有涉及文件)

**流程**(遵循 AGENTS.md「自审计用子代理」):
- [ ] **Step 1: 派 2 路并行审计子代理**(主模型,不覆盖 model 参数):
  - 子代理 A(鲁棒性轴):bridge 重试/schema/健康文件原子性/降级链/重探逻辑——找崩溃路径、类型漂移、缓存污染
  - 子代理 B(对话语义轴):NeteaseService 素材正确性、配额语义、时段门控与 TopicPicker 集成、daemon 注入、monitor 集成——对照 spec 逐条核对
- [ ] **Step 2: 汇总审计问题**,按 Important/Minor 分级
- [ ] **Step 3: 修复确认的 bug** + 新增回归测试
- [ ] **Step 4: 复跑全量回归**,更新 MEMORY.md 审计记录
