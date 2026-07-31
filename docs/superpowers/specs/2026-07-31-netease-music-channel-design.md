# 网易云音乐渠道增强设计(v9)

日期:2026-07-31
状态:已批准(用户逐节确认 1-4 节)

## 1. 背景与目标

v8 已实现听歌双向联动:播放记录仅作睡眠窗口内「夜间活跃反证」(play proof),不参与对话内容。doc/SYSTEM.md 第十一节的「每日推荐→破冰话题」方案自 2026-06-23 起一直挂在「topic_picker 未接入」状态。

本次目标(用户确认):
1. **对话内容源**:每日推荐 + 播放历史作为音乐话题进入对话,不只限于破冰,活跃期(非睡眠/非上课)任何触发可选
2. **鲁棒性**:登录失效检测 + 降级链、有限重试、schema 校验统一化、monitor health 集成
3. **故障角色化提及**:API 故障时在对话中显式提及(角色化台词,不含技术细节)
4. **play-proof 保留**并受益于同一桥接层加固

## 2. 已确认决策

| # | 决策 |
|---|---|
| 1 | 对话内容源 + 鲁棒性一条线做完 |
| 2 | 音乐话题经 topic_picker 放宽时段:活跃期(非睡眠/非上课)任何触发可选,不限于孤独破冰 |
| 3 | 数据源:每日推荐 + 播放历史;红心歌单暂缓(YAGNI) |
| 4 | 鲁棒性:登录失效检测 + 降级链、有限重试、schema 校验统一化、monitor health 扩展 |
| 5 | 故障角色化提及:音乐话题被替换为「音乐服务异常」角色化话题 |
| 6 | 每日推荐与播放历史**共享同一配额**,每次选中时**随机选一个来源** |
| 7 | 故障提及即时(话题随时可被选中),但消息发送仍受静默窗口/发送门控 |
| 8 | 架构:方案 A 分层——桥接层纯数据面 + 新增 chiguo_netease.py 策略层 |
| 9 | 写完代码完善文档,然后全面审计代码 |

## 3. 架构

```
┌─ 数据面 (零状态) ──────────────────────────────┐
│ netease_bridge.py                              │
│  · fetch_daily_songs(每日推荐)                  │
│  · fetch_recent_play(播放历史, play-proof 共用) │
│  · _api_get 统一加固:有限重试 + schema 逐条过滤 │
└──────────────┬──────────────────────────────────┘
               │
┌──────────────▼──────────────────────────────────┐
│ 策略面 (新文件 chiguo_netease.py)                │
│ NeteaseService                                  │
│  · 健康状态 → 持久化 netease_health.json        │
│  · 登录失效检测:301/401 → mark_logged_out       │
│  · 降级链:新鲜缓存 → 过期缓存 → 跳过话题        │
│  · 共享日配额 + 随机选源                         │
│  · music_topic(now):时段门控→配额→素材组装      │
└──────┬──────────────────┬───────────────────────┘
       │                  │
┌──────▼──────┐    ┌──────▼───────────┐
│ TopicPicker │    │ chiguo_monitor   │
│ 第8源:      │    │ health() 读       │
│ netease     │    │ netease_health   │
└─────────────┘    └──────────────────┘
```

## 4. 组件设计

### 4.1 netease_bridge.py(数据面加固)

- `_api_get` 有限重试:仅对瞬时失败(超时/5xx 空响应)重试 `retry_count` 次(默认 1),退避 `retry_backoff_seconds`(默认 2.0);非瞬时失败(登录失效/4xx/解析错误)不重试
- `fetch_daily_songs` 补 schema 逐条过滤(与 `fetch_recent_play` 同款:非 dict 条目跳过、字段类型校验),坏数据不整体崩溃
- 保留现有行为:API 不可达用过期缓存降级、失败不缓存、负年龄缓存不命中

### 4.2 chiguo_netease.py(新,策略层)

```python
class NeteaseService:
    def __init__(self, config: dict, base_dir: str): ...
    def health(self) -> dict                    # 给 monitor 用
    def music_topic(self, now) -> dict | None   # 给 TopicPicker 用
    def refresh_health(self, now) -> dict       # 定时/手动健康探针
```

**健康状态**(`netease_health.json` 持久化,原子写 `.tmp → os.replace`):

```json
{
  "api_alive": true,
  "logged_in": true,
  "last_check": "2026-07-31T21:00:00+08:00",
  "last_failure": "2026-07-31T20:30:00+08:00",
  "failure_reason": "timeout",
  "quota": {
    "music_topic_day": "2026-07-31",
    "music_topic_used": 1,
    "fault_topic_day": "2026-07-31",
    "fault_topic_used": 0
  }
}
```

- 登录失效:API 返回 301/401 → `logged_in=false` + 记录 `last_failure` → 后续评估轮直接跳过网络请求(快速失败),按 `reprobe_minutes`(默认 30)间隔重探
- 健康文件损坏/缺失 → 视为未知健康,重新探针重建,不崩溃

**配额(共享 + 随机选源)**:
- `music_topic_used`:每日推荐+播放历史共享,每天 ≤ `netease_daily_quota`(默认 2)
- 每次音乐话题选中时,从两个来源**加权随机选一个**(`netease_source_weights`,默认 [0.5, 0.5])
- 单来源不可用 → 自动用可用来源,不浪费配额
- `fault_topic_used`:故障提及独立,每天 ≤ `netease_fault_daily_quota`(默认 1)

**时段门控**(`music_topic(now)` 内部):
- 正常音乐话题:睡眠窗口内 / 上课中(`schedule_status` in_class)/ 配额用尽 → `None`(回退原话题系统)
- 故障话题:不受时段门控,但受 fault 配额;发送仍受静默窗口/发送门控(话题随时可被选中,发送时机不变)

**素材**:
- 每日推荐:「今天网易云给哥哥推荐了《X》」——歌名/歌手
- 播放历史:「哥哥最近在听《X》」——最新条目,只取歌名/歌手,不带链接(隐私克制)
- 故障:「网易云好像不理菓菓了」——角色化台词,不含技术细节

### 4.3 chiguo_topics.py(集成)

- 新增第 8 源 `_netease_music_topic(now)`,委托 `NeteaseService.music_topic(now)`
- 权重 `netease_weight`(默认 0.12)加入 `self.weights`
- NeteaseService 未注入 → 优雅降级(返回 None,不崩溃)

### 4.4 chiguo_monitor.py

- `health()` 读 `netease_health.json` → 输出 api_alive / logged_in / cache_age,与磁盘/内存同列

### 4.5 chiguo_proactive.toml

```toml
[netease]
play_cache_ttl_minutes = 15          # 现有
play_proof_window_hours = 2.0        # 现有
sleeping_confidence_factor = 0.5     # 现有
retry_count = 1                      # 瞬时失败重试次数(新)
retry_backoff_seconds = 2.0          # 重试退避(新)
reprobe_minutes = 30                 # 登录失效后重探间隔(新)

[topic_picker]
netease_weight = 0.12                # 音乐话题源权重(新)
netease_daily_quota = 2              # 音乐话题日配额(共享)(新)
netease_source_weights = [0.5, 0.5]  # 每日推荐 vs 播放历史 随机选源(新)
netease_fault_daily_quota = 1        # 故障提及日配额(新)
```

## 5. 错误处理总表

| 场景 | 行为 |
|---|---|
| API 不可达(超时/连接拒绝) | 重试 1 次 → 失败 → 降级链(新鲜缓存→过期缓存→跳过) |
| 登录失效(301/401) | `logged_in=false` + 记录失败 → 后续轮快速跳过网络请求,30 分钟后重探;故障话题配额内可被选中 |
| 坏数据(schema 不符) | 逐条过滤,不整体崩溃 |
| 两个源都不可用且无缓存 | `music_topic` 返回 None,回退原话题系统(不阻塞) |
| 配额用尽 | 返回 None,回退原话题系统 |
| 睡眠/上课时段 | 正常音乐话题返回 None;故障话题仍可用(配额内) |
| 健康文件损坏/缺失 | 视为未知健康,重新探针,重建文件(不崩溃) |

## 6. 测试

遵循现有 standalone runner + 临时目录 + monkeypatch 模式(不触真实 API):

- `test_netease_service.py`(新,预计 ~20 用例):
  - 配额共享与跨天重置、随机选源(seed 固定)、单源不可用自动换源
  - 时段门控(睡眠/上课 → None;故障话题豁免)
  - 登录失效检测(301 → logged_in=false + 快速跳过 + 重探)
  - 健康文件读写/损坏重建/原子写
  - 故障话题配额与角色化台词
- `test_netease_proof.py`(扩展 +4 左右):重试行为、`fetch_daily_songs` schema 过滤
- `test_topics.py`(扩展 +2 左右):第 8 源接入、权重存在、NeteaseService 未注入时优雅降级
- 全量回归:17→18 个 runner

## 7. 红线

- 不触真实 API(测试全 monkeypatch)、不写生产状态(临时目录)
- 决策引擎保持零 LLM
- 不修改 `/root/.openclaw/` 下文件
- 不使用 Python 3.14 以外语法(项目惯例:bracketless except 等)

## 8. 实施后

1. 完善文档:doc/SYSTEM.md(§2.11 扩展 + 新模块表 + §十一更新)、doc/README.md、doc/IMPROVE.md 记 v9 变更、MEMORY.md 记日志
2. 全面审计代码(遵循 AGENTS.md:dispatch 并行审计子代理 + 自审)
