# 改进清单

> 版本: 2026-07-31 | 全量代码审计完成（4 路并行 agent + 1 轮交叉复核），发现 6 CRITICAL + 14 MAJOR + 30+ MINOR，TOP5 及遗留 MAJOR 全部修复（见下方 2026-07-31 记录）

---

## 2026-08-01 — v10 多机可移植性修复（GitHub 化：路径解耦 + deploy.sh + 测试隔离）

**背景**：项目从本地搬到 GitHub private 仓库，实际运行机为另一台（pull 后运行）。审计发现 5 处硬编码 `/root/.openclaw/...` 与若干 cwd 依赖。

- **路径解耦**：`chiguo_proactive.toml` 的 `personality_source`/`lancedb_path` 与 4 处代码默认值（memory_bridge:44、chiguo_watchdog:203、chiguo_monitor:58/76、chiguo_daemon:699）全部从 `/root/...` 改为 `~/.openclaw/...`，并在读取点统一 `os.path.expanduser`（daemon personality_dir、MemoryBridge 构造、monitor `_lancedb_path`、watchdog `check_lancedb`）——换机器/换用户无需改代码，只改 toml 或直接继承默认
- **monitor lancedb 配置不一致修复**：`_load_monitor_config` 原只读 `[monitor]` 段（toml:272 注释却声称读主配置）；现 `[monitor]` 未定义 `lancedb_path` 时回退 `[memory]` 段
- **daemon 子命令 cwd 锚定**：`--conversation/--export` 与 `--stats/--alerts/--monitor` 分支原 `ChiguoMonitor()`/`AlertManager()` 无参构造（cwd 相对）→ 现先建 `DecisionEngine()` 并以 `engine._base_dir` 锚定全部路径，从任意 cwd 运行读写项目文件
- **netease_bridge QR cwd 可配**：`/opt/netease-api` → `os.environ.get("NETEASE_QR_CWD", "/opt/netease-api")`（仅终端 ASCII 二维码增强，失败被吞）
- **Python 版本 pin 进仓库**：`.python-version`（3.14）取消 gitignore 并提交——3.14-only 语法，新机器 uv 据此装对版本
- **清理 stale 产物**：`schedule_cache.json.v1.bak` 出仓（git rm + `schedule_cache.json*` ignore）
- **测试隔离加固**：4 处复制真实 toml 的用例（test_integration setup / test_escape_valve 4× / test_netease_proof `_make_engine`）在写入前把 `lancedb_path` 替换为临时目录——防止新机器上 `~/.openclaw/memory/lancedb-pro` 真实存在时测试连到生产记忆库（只读但 random_memory 会漂移断言）；test_followup 已隔离无需改
- **deploy.sh 新增**：目标机一键部署——uv+Python 3.14+venv、lancedb 可选检查、18 测试全量自检、OpenClaw skill 目录/网易云 cookie/状态文件迁移提示、openclaw cron 注册指引

**验证**：18 文件全量回归通过（338 tests）；`bash deploy.sh` 在本机跑通（Python 3.14.6）；`chiguo_daemon.py --stats --alerts --monitor` 从 /tmp 运行正确读写项目文件。

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_proactive.toml` | :9/:16 路径 `~/.openclaw/...` |
| 2 | `memory_bridge.py` | +import os；默认路径 `~` + expanduser |
| 3 | `chiguo_watchdog.py` | check_lancedb 默认 `~` + connect 前 expanduser |
| 4 | `chiguo_monitor.py` | 默认 `~`；`_lancedb_path` expanduser；`[monitor]`→`[memory]` 回退 |
| 5 | `chiguo_daemon.py` | :699 expanduser；--conversation/--stats/--alerts 分支 base_dir 锚定 |
| 6 | `netease_bridge.py` | QR cwd 环境变量 NETEASE_QR_CWD |
| 7 | `.python-version`（新） | pin 3.14，移出 gitignore |
| 8 | `.gitignore` | 去 `.python-version`；`schedule_cache.json*` |
| 9 | `schedule_cache.json.v1.bak` | git rm（过时产物） |
| 10 | `test_integration.py` / `test_escape_valve.py` / `test_netease_proof.py` / `test_feedback.py` | toml 副本注入 lancedb_path 隔离（共 5 处复制点全覆盖） |
| 11 | `deploy.sh`（新） | 目标机一键部署/自检 |
| 12 | `doc/SYSTEM.md`/`doc/README.md`/`doc/OPENCLAW_INTEGRATION.md`/`AGENTS.md`/`CLAUDE_CODE_RULES.md` | 路径说明/部署章节/`<仓库目录>` 化 |

## 2026-08-01 — v9 网易云渠道增强全面审计修复轮（F-1..F-5）

**18/18 测试文件全过（338 tests：+4，uv run python, Python 3.14，退出码 0；测试后项目根无 recent_play_cache.json/netease_health.json 残留）**

- **F-1（Important，A I-1）非法 toml 数值 → daemon 崩溃**：`NeteaseService.__init__` 的 5 处裸 `int()/float()` 强转（retry_count/retry_backoff_seconds/reprobe_minutes/netease_daily_quota/netease_fault_daily_quota）对 `"abc"` 等非法值抛 ValueError，daemon 构造（:73）与热重载（:122）崩溃。修复：新增 `_cfg_int`/`_cfg_float` 静态助手——try/except(ValueError, TypeError) 回退默认、合法负值钳制为 0，5 处调用全量替换
- **F-2（Important，A I-2 + B-2）测试污染生产缓存 + 套件 RED**：daemon-evaluate 用例的**真实** DecisionEngine → NeteaseService → `_pick_and_fetch` → `fetch_recent_play(limit=20, ttl=15, now=now)` 无 cache_file 注入，写生产 `RECENT_PLAY_CACHE_FILE`；daemon `_check_play_proof` 同样直取默认路径。修复（系统级隔离，不靠测试 hack）：① `_fetch_source_topic` recent 分支注入 `cache_file=str(self.base_dir / "recent_play_cache.json")` ② `_check_play_proof` 注入 `cache_file=str(self._base_dir / "recent_play_cache.json")`（参数不变，ttl 仍取 toml）③ 删除被测试假数据污染的项目根 `recent_play_cache.json`（内容 `{"fetched_at":"2026-07-31T23:12:25.878282+08:00","plays":[{"playTime":1722441600000,"name":"歌","artist":"手"}]}`，备份于 `/root/openclaw-tmp/opencode/audit_backup/recent_play_cache.json.polluted`）④ `test_cache_path_injection` 断言改为「测试前后生产文件逐字节不变」（快照语义，允许生产文件合法存在）⑤ 其余 daemon-evaluate 用例经 `_make_engine(临时 config_path)` 自动隔离（base_dir=临时目录）
- **F-3（Important，A I-3）非 dict 响应 → AttributeError**：`fetch_daily_songs` 的 `resp.get("code")`/`resp.get("data",{}).get("dailySongs")` 与 `check_health` 的 `status.get("data",{})`/`account.get("id",0)` 对 list 等非 dict 抛 AttributeError。修复：仿 fetch_recent_play 加 isinstance 守卫——fetch_daily_songs 非 dict → stderr 诊断 + None（不写缓存）；check_health 的 status/data/account/profile 非 dict → 安全降级「api_alive=True、logged_in=False」不崩
- **F-4（Important，B-1）仅 netease 跨触发**：话题注入原来只覆盖 `lonely_low/lonely_mid`。新增 `TopicPicker.pick_netease_only(now)`（仅尝试 netease 源，选中即 consume——与 pick 选中后消费语义一致，时段门控由 peek 内部门控完成）；daemon 注入段加 elif：其余触发（special/morning/night/meal/memory/anxiety/playful）在活跃时段额外尝试 netease 源。**排除列表**：`follow_up`/`reflect`（已有专用素材注入路径）、`lonely_high`（崩溃态）、`longing`（逃生阀破防，含 escape_valve 与非逃生阀自然 longing——不夹带音乐话题）；lonely_low/mid 由原分支覆盖
- **F-5（Minor，B 语义风险 4）fault 素材自称「菓菓」**：与角色铁律②冲突（不称自己为菓菓，用我）。hint 改为「网易云好像不理我了，跟哥哥念叨一句音乐服务不太给力」；顺带修 recent 素材同类问题（「菓菓想跟着听听看」→「我想跟着听听看」）
- **测试（338 = 334 + 4）**：`test_netease_service.py` 28→30——`test_config_invalid_values_fallback`（"abc"/None 回退默认、负值钳 0、weights 非法回退 [0.5,0.5]）、`test_check_health_non_dict_resp_degrades`（status/data/account/profile 非 dict 降级不崩）；`test_netease_proof.py` 29→31——`test_daily_songs_non_dict_resp`（resp=[1,2,3] → None 且不写缓存）、`test_pick_netease_only_injection_rules`（monkeypatch pick_netease_only：playful → 调用 1 次且注入 netease topic；lonely_low → 不调用；follow_up/reflect/lonely_high/longing → 不调用不注入）
- **顺带修复（验证中发现，非审计项）test_escape_valve.py 夜间时段炸弹**：0-8 点运行必挂（配置静默窗口 0-8 + `datetime.now(CST)`；端到端用例夜间真实 Bayesian 给 sleeping 高置信 → sleeping_guard）。按同文件既有去时段敏感模式修复：can_send 两用例 `set_quiet_window(0,0)` 空窗口；`test_end_to_end_escape_valve_send` 伪造非 sleeping infer_user_state（confidence 0.3）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_netease.py` | F-1 `_cfg_int/_cfg_float` 兜底；F-2 recent 缓存 base_dir 锚定；F-5 hint 菓菓→我（fault + recent） |
| 2 | `netease_bridge.py` | F-3 fetch_daily_songs/check_health 非 dict 守卫与降级 |
| 3 | `chiguo_topics.py` | F-4 新增 pick_netease_only |
| 4 | `chiguo_daemon.py` | F-2 _check_play_proof 缓存锚定；F-4 非孤独触发 elif 注入（排除 follow_up/reflect/lonely_high/longing） |
| 5 | `test_netease_service.py` | +2 用例（28→30） |
| 6 | `test_netease_proof.py` | test_cache_path_injection 快照化 +2 用例（29→31） |
| 7 | `test_escape_valve.py` | 3 处夜间时段炸弹去敏感（沿用文件既有模式） |
| 8 | `MEMORY.md` / `doc/IMPROVE.md` | 审计修复轮记录 |

**验证**：18 个 runner 全量回归通过（338 tests）；链尾 CHAIN_EXIT=0；测试后项目根无 `recent_play_cache.json`/`netease_health.json` 残留；未运行无参数 `chiguo_daemon.py`；未触碰 `/root/.openclaw/`。

## 2026-07-31 — v9 Task 3 代码评审修复轮（两阶段接口 peek/consume + fail-closed 门控）

**18/18 测试文件全过（334 tests：test_topics 20→23 + test_netease_service 24→28 + 既有 327，uv run python, Python 3.14，退出码 0）**

- **Important — 配额在候选生成时消费而非选中时**：原 `pick()` 无条件调 `music_topic`，真实 service 在 `_pick_and_fetch` 内即消费 quota（每天 ~6 次 pick 会把配额 2 耗尽，netease 实际发出期望仅 0.24/天，违背「每天约 1-2 次音乐话题」意图）。修复为两阶段：
  - `chiguo_netease.py` 新增 `peek_music_topic`（同逻辑不消费配额：fault 分支内联、配额只读；拉取成功仍 `_sync_success` 恢复健康——该操作不消费配额且「拉取成功=API 正常」是事实；两源全失败仍 `refresh_health` 探针）、`consume_music_topic`/`consume_fault_topic`（选中后确认消费）；`_pick_and_fetch(now, consume=True)` 加开关（peek 路径跳过 `_consume_music`）
  - `music_topic` = peek + consume 包装（返回话题即已消费，供非 TopicPicker 调用方，语义与既有版本一致——既有 24 用例全过）；删除被内联取代的 `_fault_topic`
  - `chiguo_topics.py`：`_netease_music_topic` 委托 `peek_music_topic`；`pick()` 抽选后命中 netease_music/netease_fault 才 `consume_*_topic`（try/except 兜底）
- **Minor — fail-open 门控守卫**：schedule_status/quiet_window 异常时原默认 False（可能上课/睡眠误发）→ 改为 return None（fail-closed）
- **Minor — 死断言**：`test_pick_valid_set_includes_netease` 合法集合移除 fake 恒不产出的 `netease_fault`（fault 类型由 `test_netease_fault_consume_when_selected` 覆盖）
- 测试：`test_topics.py` 20→23（FakeNeteaseService 两阶段化；新增「选中才消费」：300 种子 peek=300/选中=37/consume=37 且 consume<peek；fault 高权重 100/100 选中 consume_fault==选中数；门控异常 fail-closed 不发不调）；`test_netease_service.py` 24→28（peek 不消费配额可重复 peek/耗尽后 None、peek fault 不消费、peek 两源全失败走探针、music_topic=peek+consume 配额行为不变）
- Task 3 修复轮时点未触及 `chiguo_daemon.py`（策略层接线由 Task 4 完成，见下节 Task 3 记录）；文档计数记录实际数字（统一修正归 Task 6）

**验证**：18 个 runner 全量回归通过（334 tests，18 文件：26+7+17+42+10+19+18+10+8+8+15+10+16+23+34+14+29+28）；仅测试与文档核对，未运行无参数 `chiguo_daemon.py`（避免写生产状态）。

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_netease.py` | 两阶段接口；music_topic 重构为 peek+consume；_pick_and_fetch 加 consume 开关；删 _fault_topic |
| 2 | `chiguo_topics.py` | _netease_music_topic 改 peek + fail-closed；pick() 抽选后确认消费 |
| 3 | `test_topics.py` | FakeNeteaseService 两阶段；gate_args 改 peek_calls；valid 集合去 fault；+3 用例（20→23） |
| 4 | `test_netease_service.py` | +4 用例（24→28） |

## 2026-07-31 — v9 Task 3：TopicPicker 第 8 源接入（netease 策略层委托）

**18/18 测试文件全过（327 tests：test_topics 14→20 + 既有 321，uv run python, Python 3.14，退出码 0）**

- `chiguo_topics.py` — TopicPicker 话题源 7→8（新增 netease 第 8 源）：
  - `__init__(self, state, config, netease_service=None)` — 策略层可注入可省略（None → 静默跳过，向后兼容）；daemon 构造（chiguo_daemon.py:73-75）与热重载分支（:121-124）已注入 NeteaseService（Task 4 完成，见下）
  - weights 新增 `"netease": config.get("netease_weight", 0.12)`
  - `pick()` 在 pref 候选后、weather/general 兜底前追加 netease 候选
  - 新增 `_netease_music_topic(now)`：自行计算时段门控（`schedule_status` 的 in_class + `cooldown.quiet_window()` + 复用 `_in_quiet_window` 跨午夜语义）后委托 `netease_service.music_topic(now, in_class, in_quiet_window)`；schedule_status/quiet_window/music_topic 三处各自 try/except 兜底 → 任何异常静默跳过不阻塞话题选择
  - 导入 `NeteaseService, _in_quiet_window`（前者仅类型参考，duck typing 注入）
- `chiguo_proactive.toml` — `[netease]` 段 +3 键（retry_count=1 / retry_backoff_seconds=2.0 / reprobe_minutes=30）；`[topic_picker]` 段 +4 键（netease_weight=0.12 / netease_daily_quota=2 / netease_source_weights=[0.5,0.5] / netease_fault_daily_quota=1）
- 测试：`test_topics.py` 14→20 用例（MockState.cooldown 补 quiet_window=lambda: (0,8)；新增：netease 权重来自真实 toml、注入 fake service 300 种子必现 netease_music、music_topic 收到与 state 一致的 in_class/in_quiet_window 门控参数（上课日 14:00 → (True,False)、非上课 03:00 → (False,True)）、未注入不崩且结果无 netease 类型、music_topic 抛异常静默跳过、注入后 300 种子结果均落在含 netease_music/netease_fault 的独立合法集合）
- **Task 4 接线（v9）**：`chiguo_daemon.py` 构造（:73-75）注入 `NeteaseService(self.config, str(self._base_dir))` 并传给 TopicPicker；热重载分支（:121-124）检测 toml mtime 变化后同步重建策略层与 TopicPicker（重试/配额参数可能被改）；未运行无参数 chiguo_daemon.py

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_topics.py` | 8 源：docstring/import/__init__ 可选 netease_service/weights 新键/pick 追加候选/新增 `_netease_music_topic` |
| 2 | `chiguo_proactive.toml` | `[netease]` +3 v9 键、`[topic_picker]` +4 v9 键 |
| 3 | `test_topics.py` | MockState quiet_window + 6 新用例（14→20） |

## 2026-07-31 — v9 Task 2 代码评审修复轮（策略层 chiguo_netease + test_netease_service）

**18/18 测试文件全过（321 tests：test_netease_service 20→24 + 既有 297，uv run python, Python 3.14）**

- **Important — 抓取期失败不进故障态**：`_pick_and_fetch` 两源全失败后调用 `refresh_health(now)` 真实探针（本轮仍返回 None 不消费配额；API 宕机/登录失效 → 置 faulty，下一次 `music_topic` 走故障话题分支受 fault 配额约束；探针 healthy（数据为空但 API 正常）→ 保持静默回退不误标故障）。新增 2 用例（失败置故障下一轮产故障话题 / healthy 探针两次 None 无故障话题）
- **Important — `_in_quiet_window` 死代码**：docstring 注明「供 chiguo_topics（Task 3 TopicPicker）导入复用，勿删」
- **Minor — `_load_health` 值类型不校验**：`"quota_music_used": "abc"` 会让 `_consume_music` 抛 TypeError 违背「不崩溃」。加载时对配额字段（int 且 ≥0，排除 bool）与布尔字段做类型守卫，非法值回退默认。新增脏健康文件测试（加载回退 + 合法值保留 + 消费不崩）
- **Minor — 随机选源分布断言偏弱**：抽样 200 次仅断言两源均出现 → 加强为 seed 42 固定、抽样 2000 次、daily 比例断言 0.5±0.08（实测 969，余量充足）
- **Minor — source_weights 允许负权重**：`[1,-5]` 产生退化分布 → 钳制非负（`max(0.0, w)`），两权重全 ≤0 回退 [0.5,0.5]。新增钳制测试
- **Minor — `health()` docstring 误导**：「快照（引用，只读）」→「给 monitor 的快照副本（浅拷贝）」
- 记录未修（既有约定）：#2 check_health 契约、#3 构造副作用多实例 last-wins（项目单实例约定）、#4 配额无锁（单线程语义）、#8 last_failure 语义（`_set_faulty` 注释已有「只在转为故障时记录首次失败时刻」）
- 连带：`test_both_sources_down_no_quota` 补探针 patch（新失败路径会调 check_health，避免真实网络）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_netease.py` | `_pick_and_fetch` 两源全失败后 `refresh_health` 探针；`_load_health` 值类型守卫；source_weights 负权重钳制；`_in_quiet_window`/`health()` docstring |
| 2 | `test_netease_service.py` | 20→24 用例：随机选源 2000 次比例断言、抓取失败置故障、healthy 探针静默、脏健康文件、负权重钳制；`test_both_sources_down_no_quota` 补探针 |

## 2026-07-31 — v9 网易云音乐渠道增强（Task 2: 策略层）

**18/18 测试文件全过（317 tests：新增 test_netease_service 20 用例 + 既有 297，uv run python, Python 3.14）**

- `chiguo_netease.py` — 新增 **网易云策略层** `NeteaseService`（依赖 netease_bridge 数据面，不依赖 chiguo_daemon，零 LLM）：
  - **健康文件** `netease_health.json`（base_dir 锚定，原子写 tmp→os.replace，损坏/缺失/结构不符 → 默认重建不崩溃）；`refresh_health()` 真实探针（api_alive=False → faulty=unreachable；api_alive 但未登录 → faulty=login_expired；均 OK → 恢复并清 last_failure）
  - **降级链**：faulty 且未到 reprobe_minutes → 快速跳过网络请求直出故障话题（netease_fault，不受上课/睡眠时段门控，受独立 fault 日配额 1）；faulty 话题 hint 为"音乐服务不太给力"，data 仅 {source, reason}
  - **共享日配额**：音乐配额（默认 2/日）两源共享、跨天重置、双源全挂不消费；拉取成功 → `_sync_success` 自动恢复 faulty
  - **随机选源**：`netease_source_weights`（默认 [0.5, 0.5]）加权随机选 daily（每日推荐）/recent（最近播放，取 playTime 最大者）；选中源不可用自动换另一源
  - **素材安全**：话题 data 均不含 share_url/链接字段（文案由 OpenClaw LLM 生成，链接留待发送层按需拼接）
- 测试：`test_netease_service.py` 新增 20 用例（健康文件缺失/损坏/原子写持久化、配额共享+跨天、随机选源抽样 200、换源兜底、双源全挂、时段门控、故障绕过门控+配额、登录失效检测、重探间隔、恢复、素材无链接、最新播放、naive tz、源权重配置）；monkeypatch bridge 三函数 try/finally 恢复
- 文档同步：doc/SYSTEM.md（模块依赖图 + chiguo_netease、文件清单 + netease_health.json、测试表 17→18 文件 297→317）、doc/README.md（文件结构 + 测试计数）

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_netease.py` **新增** | 网易云策略层 `NeteaseService`（健康探针/降级链/双配额/随机选源/素材组装，详见上文） |
| 2 | `test_netease_service.py` **新增** | 20 用例（健康文件/配额/门控/降级/素材，详见上文） |

## 2026-07-31 — v8 用户状态渠道增强：双作息学习 + 听歌双向联动（含审计修复轮）

**17/17 测试文件全过（297 tests：test_circadian 31→34 + test_netease_proof 22→24 + 其余 239，uv run python, Python 3.14）**

**审计修复轮（2 路并行审计 + 4 项修复）**：
- **Important — loop 模式桶翻转门禁陈旧**：`evaluate()` 从不调 `_sync_quiet_window`，--loop 进程启动于工作日桶后跨桶翻转（周五 20:00）门禁仍用旧窗口直到下条用户消息 → evaluate() 在 can_send 前加 `self.state._sync_quiet_window(now)`（幂等），新增回归测试
- **Minor — netease_bridge 非 dict 崩溃**：fetch_recent_play 对 resp/data 非 dict 抛 AttributeError，违反"解析失败 → None"契约 → isinstance 守卫
- **Minor — 迁移补桶识别节假日/调休**：补桶从纯 `weekday()<5` 启发式改为 `is_makeup_workday → weekday` / `is_holiday → weekend` / 否则 weekday()<5（holidays.json 含 2026 数据，真实日期测试：国庆 10-01→weekend、调休 10-10→weekday）
- **Minor — 数值字段类型漂移**：`_sync_quiet_window` 对字符串 conf 比较抛 TypeError → bucket_window 返回值 int()/float() 强转 + 失败回退 (0,8,0.0)；迁移门控 confidence 强转
- 遗留权衡（未修，见设计取舍）：挂机播放污染（autoplay 夜间 → play_proof 误压制 sleeping，频率受评估节奏 + TTL 约束）
- 过程教训：冒烟运行真实 daemon 曾污染生产 state（2 条未实际发送的 send 决策）→ 官方 `--send-result failed` 退款恢复；后续验证只用测试与 `--status`

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_circadian.py` | **双作息分桶**：新增 `bucket_for(dt, is_holiday, is_makeup_workday, weekend_start_hour=20, weekend_end_hour=20)`（调休上班日→weekday 优先于节假日，节假日→weekend，周五 20:00 后/周六全天/周日 20:00 前→weekend）；`track_reply_hour` 条目带 `bucket` 字段（同日不同桶分条目，如周五 20:00 前后）；`CircadianTracker` 双桶字段（weekday_*/weekend_* 独立窗口+置信度）+ `active_days`（听歌活跃记录，同结构）+ `bucket_window`/`set_active_bucket`（兼容字段 quiet_start/end/confidence = 当前生效桶快照）；`recompute` 两桶独立估计（reply+active 逐小时合并计数，桶内 sample_days 日期去重，数据不足不覆盖） |
| 2 | `chiguo_state.py` | `on_user_message` 按当日 `bucket_for` 分桶记录；`_sync_quiet_window(now=None)` 按当前时刻选桶（置信度达标应用该桶学习窗口，否则该桶回退默认 0-8）；`_migrate_circadian_v8()` 迁移（无 bucket 条目按 `weekday()<5` 启发式补桶，解析失败丢弃；weekday_* 与 weekend_* 全默认且旧 confidence>0 才继承旧窗口到 weekday_*——防止 v8 周末快照误继承）；`STATE_VERSION` 7→8 |
| 3 | `netease_bridge.py` | 新增 `fetch_recent_play(limit=20, ttl_minutes=15, now=None, cache_file=None)`：GET /user/record?type=1&limit=20（近一周播放记录），独立缓存 `recent_play_cache.json`（原子写；负年龄/损坏缓存不命中；API 失败/未登录不缓存） |
| 4 | `chiguo_daemon.py` | 新增 `_in_quiet_window` + `_check_play_proof(now)`：仅生效睡眠窗口内拉取；2h 证据窗（play_proof_window_hours）内播放时刻在窗口内才 `record_active`（**按播放时刻分桶**，跨午夜/周五边缘不污染双桶）；反证后 recompute + `_sync_quiet_window` 反向校正；evaluate() sleeping 阻塞与逃生阀 sleeping_guard 改用 `effective_conf = 原始置信度 × sleeping_confidence_factor`（play_proof 时，默认 0.5） |
| 5 | `chiguo_proactive.toml` | 新增 `[netease]` 段：play_cache_ttl_minutes=15 / play_proof_window_hours=2.0 / sleeping_confidence_factor=0.5；`[circadian]` 参数两桶共用（不新增键） |
| 6 | `test_circadian.py` | 18→31 用例：分桶纯函数（调休/假期/周五 20:00 边界/周日晚边界）、同日跨桶分条目、按桶过滤聚合、双桶独立估计、record_active 合并计数、bucket_window/set_active_bucket、on_user_message 按日分桶、迁移补桶 + 防周末快照误继承、按当前时刻选桶 + 未达标回退 |
| 7 | `test_netease_proof.py` **新增** | 22 用例：bridge 12（成功解析/limit 传播/缓存命中与副本/缓存过期/API 失败与损坏缓存/非法 playTime 过滤/缓存路径注入/未登录/未来 fetched_at 不命中/song 非 dict/naive tz 补齐/默认 now 冒烟）+ daemon 10（_in_quiet_window 边界/窗口内拉取/recompute 触发/按播放时刻分桶/窗口外不拉取/过期播放无证据/fetch None 或异常降级/窗口外播放不算/播放证据压制 sleeping 置信度/toml 系数驱动 effective） |
| 8 | `test_followup.py` | 14 用例不变（v8 未触及接话茬路径） |

**设计取舍**：
- **播放反证只做"窗口内时间"证据**，不做歌单/歌曲情绪分析（需 LLM，违背决策引擎零 LLM 铁律，YAGNI 砍掉）
- **record_active 按播放时刻分桶而非评估时刻**：19:30 播放、21:30 评估的跨桶场景（周五窗口边缘/跨午夜）记错桶会污染双桶学习
- **sleeping 置信度压制系数可调**（`[netease] sleeping_confidence_factor`，默认 0.5）：只压 Bayesian 置信度、不硬编码放行，压到阻塞阈值以下自然放行；反证只在评估时点生效，不持久化"醒着"状态（避免状态污染）
- **全链路降级**：未登录/API 不可用/缓存损坏 → 本轮跳过反证，不阻塞不告警；缓存 TTL 15 分钟防每轮评估打 API；挂机播放风险由低频率 + 置信度门槛天然保护
- 双作息参数两桶共用（min_sample_days=7 对周末偏严 → 约 3-4 周才激活周末窗口），不新增参数保持配置面小

**验证**：17 个 runner 全量回归通过（297 tests）；test_netease_proof 用 monkeypatch/fake 服务与固定 CST 时间，不触真实 API；未运行无参数 `chiguo_daemon.py`（避免写生产状态），仅测试与文档核对。

---

## 2026-07-31 — v7 拟人化增强：生物钟学习 + 接话茬（含审计修复轮）

**16/16 测试文件全过（249 tests：新增 test_circadian 18 + test_followup 14，uv run python, Python 3.14）**

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_circadian.py` **新增** | 生物钟学习：`estimate_sleep_window` 对 24 小时回复计数做环形滑动窗口（width ∈ [min_width=5, max_width=12]），(sum, width, start) 字典序取最小者（确定性）；置信度 = 完整度(sample_days/history_days) × 安静度(1 − 窗口均回复/全天均回复)；`CircadianTracker`（reply_days/quiet_start/quiet_end/confidence/sample_days）持久化到 state；损坏数据（非法日期/越界小时/非列表 hours/非 dict 条目）逐条防护 |
| 2 | `chiguo_state.py` | `STATE_VERSION` 6→7；`circadian` 字段 + `pending_topics` 列表持久化（字段白名单过滤加载，`circadian: null` 防护）；`CooldownState.quiet_window()` 访问器（发送门禁/睡眠循环统一读活动窗口）；`_sync_quiet_window()` 动态静默窗口（置信度 ≥ min_confidence=0.5 用学习窗口，否则回退配置默认 0-8）；`can_send` 静默块改读 `quiet_window()`（审计修复：学习窗口此前未作用于发送门禁）；`on_user_message` 记录回复小时 + 摄入 analysis topic / topic_resolved；新增 5 个 pending_topics 管理方法（add/resolve/mark_attempted/prune/`_cap` 20 条，含非 dict 守卫） |
| 3 | `chiguo_trigger.py` | 触发类型 12→13：`follow_up` 接话茬 —— pending 话题年龄 [2h, 48h] 窗口 + 钟形权重 `0.35 × exp(-((age-4)/3)²)`（> min_weight 0.03 才成候选）；**全部窗口内话题逐个加候选**（审计修复：原只取第一个，最老话题权重过低会饿死更年轻话题）；无 pending 时近 48h 用户相关记忆兜底（source=memory，不落盘）；触发后标记 attempted 单次尝试；过期顺带清理；`_is_free_time` 改读 `quiet_window()` |
| 4 | `chiguo_daemon.py` | `_build_context` 输出 `context.follow_up`（topic/source/age_hours）+ guidance【接话茬】提示 + instruction 素材注入（聊天式提起、不要汇报腔）；热重载 `_maybe_reload_config` 后重新 `_sync_quiet_window()` 防窗口陈旧；`_idle_reason`/`_dynamic_sleep_interval`/`_estimate_next_check` 改读 `quiet_window()`（审计修复：睡眠循环与门禁一致） |
| 5 | `chiguo_proactive.toml` | 新增 `[circadian]` 段（history_days=14/min_sample_days=7/min_confidence=0.5/min_width=5/max_width=12）+ `[trigger]` 段 follow_up_* 6 键（审计修复：min_confidence 0.6→0.5，与 min_sample_days=7 匹配，7 天数据可激活学习窗口） |
| 6 | `test_circadian.py` **新增** | 18 用例：环形滑动窗口最优性/跨午夜/置信度公式/min_confidence 门槛/数据不足回退/损坏数据防护/滚动窗口修剪/学习窗口作用于 can_send（睡眠中禁止、窗口外放行） |
| 7 | `test_followup.py` **新增** | 14 用例：pending 摄入与去重/过期清理/attempted 单次尝试/钟形权重峰值/记忆兜底（FakeBridge）/下限权重不候选/多话题年轻者胜出/daemon context 集成 |
| 8 | `test_escape_valve.py`/`test_feedback.py` | 审计修复配套：config 置空窗口后补 `engine.state._sync_quiet_window()`，门禁改读活动窗口后保持测试小时无关性（凌晨运行不挂） |

**设计取舍**：
- **固定复盘被砍**：原计划"定时复盘/总结"排程被取消 —— 拟人 = 真实信号驱动而非固定排程；用"接话茬（follow_up）"让话题续接由真实对话信号（analysis topic）驱动，时间由钟形权重自然选择，避免机械定时的"汇报腔"
- 睡眠窗口同样由真实回复时间信号学习（生物钟），置信度门槛保证冷启动/异常作息时回退配置默认 0-8，不引入固定假设
- 单次尝试 + 过期清理：接话茬只试一次，避免同一话题反复刷屏；pending_topics 上限 20 条防状态膨胀
- 审计修复：学习窗口统一作用于 silent_hours/发送门禁/睡眠循环三处（原仅 silent_hours），杜绝"学习到用户在睡却仍发消息"的矛盾

**验证**：16 个 runner 全量回归通过（249 tests）；新测试独立运行（tempfile 隔离 + 固定种子），test_followup 含 FakeBridge（lancedb 不可用）兜底路径。

---

## 2026-07-31 — 监控/工具链层 7 项回归审计修复（alerts 崩溃 / 路径锚定 / watchdog 重启误报）

**39/39 monitor + 14/14 topics + 17/17 integration + 15/15 escape_valve + 全量 14 测试文件通过 + 4 组临时脚本实测（uv run python, Python 3.14）**

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_monitor.py` | **MAJOR**：`alerts()` 对 `state:null`/`cooldown:null`/`emotion:null` 条目 `.get()` 链 AttributeError 崩溃（stats() 有归一化而 alerts() 没有，不对称）。抽出 `_normalize_entry()` 公共归一化（state/context 非 dict → {}，emotion/cooldown 非 dict → {}），stats() 与 alerts() 共用；顺带修复 B5 `tracked` 的硬索引 KeyError（缺 `messages_without_reply` 键时崩溃，临时脚本实测抓出） |
| 2 | `chiguo_monitor.py` | 死变量 `sends_with_latency`（声明后从未使用）删除 |
| 3 | `chiguo_monitor.py` | 回复率口径对齐：stats() 的 `reply_events` 对非数值 mwr 存 None（原强转 0，`prev=3, curr=None→0` 会误计一次回复），比较处与 alerts() B5 一致做双方 isinstance 检查（None/非数值视为未知，不计回复变化） |
| 4 | `chiguo_monitor.py` | `_load_monitor_config` 相对路径找不到时回退 `Path(__file__).resolve().parent`（与 health() 的 config 检测一致），避免从其他 cwd 运行阈值静默回落默认值导致 config_ok 与实际加载不一致 |
| 5 | `chiguo_watchdog.py` | tick_seq 回退（本次 < prev）视为 state 文件被删/重建后的重启：重置 `stall_since`、不告警、输出 `tick_restarted`；相等且 >3h 不增才告警停滞（向后兼容 `stall_since` 字段格式）。实测现网 `chiguo_watchdog_state.json` 的 16:41 误报在下一次运行自动自愈清除（stall_since → null） |
| 6 | `chiguo_rotation.py` | **MAJOR**：相对 `archive_dir`（toml `"archive"`/force_rotate 默认）锚定模块目录（`_anchor_archive_dir`），绝对路径原样保留——从 /tmp 运行 `force_rotate` 曾把真实日志移进 /tmp/archive。`rotate_if_needed`/`force_rotate`/`_rotate_one`/`_cleanup_archives` 全部统一处理 |
| 7 | `anniversary_manager.py` | **MAJOR**：默认路径锚定：无参构造时若 cwd 已存在同名 `anniversaries.json` 则沿用（兼容旧版/测试 chdir 隔离目录），否则锚定模块目录——从 /tmp 运行 `--anniversary add` 不再写散数据；显式绝对路径原样生效。chiguo_topics.py:37 / daemon:990 无参调用自动受益 |

**验证**（/root/openclaw-tmp/opencode/verify_audit_fixes.py）：
- a. 构造含 `state:null`/`cooldown:null`/`emotion:null`/mwr 非数值字符串的日志（14 天窗口内）→ `alerts()`/`stats()` 不崩，send 计数正确，仅触发合法告警
- b. 从 /tmp cwd：`force_rotate`（相对 archive_dir）→ 归档落到 `/root/character_test/archive/`（唯一命名的临时测试文件，事后精确清理，**不触碰**项目真实归档）；`/tmp/archive` 未创建；`rotate_if_needed` 同验证；绝对 archive_dir 仍原样生效
- c. `AnniversaryManager()` 无参构造（cwd=/tmp）→ `_path` = `/root/character_test/anniversaries.json`（锚定项目根，非 /tmp）；显式绝对路径仍生效；无参构造只读不写盘
- d. watchdog：prev=10→本次=1 → ok + `tick_restarted`、stall_since 重置；prev=10=本次=10 且 stall_since=4h 前 → warn 停滞告警；prev=1→本次=5 → 误报自愈；首次运行（无 prev）不误报

---

## 2026-07-31 — 现网 schedule_cache.json 数据修复（v1 脏缓存 → v2，数据修复非代码）

**数据修复（运行时文件，非代码改动）**：现网 `schedule_cache.json` 为 v1 缓存（无 `cache_version`），且 xskb.xlsx 缺失导致 `_ensure_parsed` 无法自动重解析自愈。本次手工迁移：

- 备份原文件为 `schedule_cache.json.v1.bak`（原始证据保留）
- 重建方法（不编造数据）：每条 v1 条目的 course/teacher/weeks_raw/location 均是可追溯的原始 cell 文本片段 → 拼接回 cell → 用 `ScheduleParser._parse_cell`（v2 逻辑）重新拆分 → 主课 + `alternates`
- 结果：20 条脏条目 → 20 主课 + 32 alternates（52 门课实例）；被切断的课程名 4 条全部恢复（`安全教育(理论) 胡群【10` → 完整课程 + 2 alternates；`大学英语（二）(理论) 程东岳【2` → 完整课程 + 2 alternates）；location 残留课程文本 20/20 清零
- 写入：`.tmp` + `os.replace` 原子写（同 `_save_cache` 模式），`cache_version=2`、`parsed_at=重建时间戳`
- 验证：ScheduleParser 加载（xlsx 缺失场景）+ query 周次互斥切换（W2/W10/W19 主课 alternates 正确切换）；`rg "【"` 零残留；test_integration 17/17、test_topics 14/14、全量 14 测试文件通过
- **遗留 2 项不可无猜恢复**（v1 解析吞掉 `-` 分隔符，course/teacher/location 均不含该字符，无其他证据源）：周一 7/8 节 `安全教育(理论)` weeks 存为 `[1016]`（`1016(双)周`，推断原始 `10-16(双)周`）；周五 3/4 节 `大学英语（二）(理论)` weeks 含 `[24]`（`24,6-7,9-10,12-15,17周`，推断原始 `2-4,6-7,9-10,12-15,17周`）。保留确定性解析结果不猜测，恢复 xskb.xlsx 后自动重解析可自愈

---

## 2026-07-31 — 核心 state 层 4 项审计修复（时间戳防护 / 路径锚定 / 缓存保护）

**17/17 integration + 15/15 escape_valve + 10/10 feedback + 39/39 monitor + 14/14 topics + 7/7 holiday + 16/16 trigger 全过 + 27 项临时脚本验证（uv run python, Python 3.14）**

| # | 文件 | 改动 |
|---|------|------|
| 1 | `chiguo_state.py` | `silent_hours()`/`silent_hours_wall()` 的 `fromisoformat` 加 `try/except (ValueError, TypeError)` → 返回 999.0（与"从未交互"语义一致，实测 `last_user_message_at="garbage"` 下 can_send/tick/snapshot/availability/infer_user_state/longing_break_eligible/daemon evaluate 全不崩）。已核查全部调用方（can_send/snapshot/_idle_reason/trigger.py/generator/composer）无 `>= 999.0` 边界逻辑被破坏 |
| 2 | `chiguo_state.py` | ScheduleParser 构造改为 `self._anchored(xlsx_path)` + `cache_path=self._anchored("schedule_cache.json")`（原相对路径在 cron cwd 漂移时静默空课表）；HolidayParser 改为 `data_path=self._anchored("holidays.json")`。绝对路径配置原样保留（Path 拼接语义） |
| 3 | `holiday_parser.py` | `__init__` 覆盖文件语义收紧：显式 `data_path` 时只读它、不再回退 cwd（消除与 daemon `_check_data_freshness` 锚定 base_dir 的不一致）；无参构造（CLI/测试）保留 cwd 默认行为，向后兼容 |
| 4 | `schedule_parser.py` | **CRITICAL C2 修复**：`_parse()` 返回 bool（成功 True / 失败 False），`_ensure_parsed` 仅 `if self._parse(): self._save_cache()`。xlsx 损坏 → `_schedule={}` 降级但**不覆盖有效缓存**；失败时记住 mtime 避免每轮 query 重复解析刷 stderr，xlsx 修复后 mtime 变化自然重试。返回契约变化：`_parse` 由 None 改为 bool，仅内部使用 |
| 5 | `chiguo_state.py` | `save()` 写 .bak 后 `os.chmod(bak_path, 0o600)`（备份含隐私状态，参考 netease_bridge.py:114 模式）；`state_lock` docstring 注明"仅单线程语义，多线程需外部串行化"；`state_lock`/`_in_lock` 死代码保留不删（对外接口） |

**验证**：27 项断言全过 — ① garbage 时间戳下 11 项 state/daemon 调用不崩且 silent_hours 恒 999.0 ② `/tmp` cwd 下构造 → xlsx/cache/holidays/state 全解析到项目根，且带 data_path 时不加载 /tmp/holidays.json（无 cwd 回退），无参构造仍回退 cwd ③ corrupt xlsx + 有效 v2 缓存 → 解析失败后缓存文件内容逐字节不变（两次 _ensure_parsed 后仍原样）、内存 `_schedule={}`；正常 xlsx → True + 照常落盘 ④ 连续两次 save 后 .bak 权限实测 0o600。

---

## 2026-07-31 — 测试质量提升（弱断言强化 + trigger/topics 零覆盖补测）

**217/217 tests pass（14 文件；含 test_holiday_parser 7/7，见下 ✅）**

| # | 文件 | 改动 |
|---|------|------|
| T1 | `test_integration.py` | test_1 由纯 print 改为真断言 `trigger is None`（实测 seed=42 恒 None：lonely 候选权重 0.012<0.03、anxiety 归一化 0.171<0.3、14:00 无 ritual 窗口）；test_8 断言非 None + type 在 09:00 合法集合（实测为 lonely_low，morning 仅 10% 概率非确定性） |
| T2 | `test_escape_valve.py` | +2 用例：`test_sleeping_guard_blocks_escape_valve_at_high_confidence`（逃生阀激活 + sleeping 置信 0.95 ≥ 0.9 → idle/sleeping_guard，held_count 不累积，实测 held=7 保持 7）；`test_escape_valve_sends_when_sleeping_confidence_below_block`（0.85 对照 → 仍 send/longing/high） |
| T3 | `test_trigger.py` **新增** | 16 用例：孤独 softmax 竞争（low 15 不触发 lonely、high 75 三级竞争 low92/mid90/high18）、rate_factor 调制（lon15+rate10 → lonely_low 200/200）、anxiety 归一化（40→0/100，80→88/100）、morning(10%)/night(12%)/meal(5%) 窗口概率、playful/reflect/longing 候选与负例、special 日期、memory naive tz 防护 + 垃圾 trigger_at 不崩 + habit 窗口 |
| T4 | `test_topics.py` **新增** | 14 用例：权重分布（schedule/weather/general 可用源归一化 ≈ 40/27/33%，2000 种子）、high_rate 调制（general↑ weather↓）、人格调制（openness 90 vs 5 → memory 172 vs 106）、Ebbinghaus 纯函数（age0→1.0、S·imp 点→e⁻¹、min_weight 兜底、缺时间戳→1.0）、search_with_forgetting 排序、random_with_forgetting 新记忆 89% 优先、节气/纪念日/schedule 分支/weather+general 模板 |
| T5 | `test_chiguo_math.py` | +4 用例：sigmoid ±1000 大输入不溢出（默认陡度）、decay/recover 负半衰期守卫（均返回原值）、weighted_trigger_choice 负权重（负项永不被选 + 全非正随机回退） |

**发现并报告的缺陷（按规则不修）**：
- `sigmoid(-1000, 50, 1.0)` → `OverflowError`（math.exp(1050) 溢出）。生产路径陡度仅 0.06~0.2 不受影响，测试已固化现状并注释。
- `test_holiday_parser.py` 曾失败为**既有问题**：`holidays.json` 仅含 2027 年数据（`_generated_for=2027`），其 2026-10-01 国庆断言必然失败。**✅ 已修复**：`holidays.json` 已重新生成为 2026 官方数据，7/7 用例通过。

**测试方法**：所有概率型断言采用固定种子序列（`random.seed(seed0+i)` 逐次重播种）→ 完全确定性；概率区间以实测数据 ±3σ 设计（200~2000 种子统计，复跑 3 次稳定）。

---

## 2026-07-31 — daemon 遗留 4 项修复（cwd 隔离 / CLI 语义）

**17/17 integration + 10/10 feedback + 13/13 escape_valve + 39/39 monitor 全过 + 跨目录 CLI 冒烟通过（uv run python, Python 3.14）**

| # | 问题 | 修复 |
|---|------|------|
| m1 | `chiguo_daemon.py:33` 模块级 `os.chdir()`：import 即劫持调用方 cwd（任何导入方进程工作目录被静默改变） | 删除该行（v8）；`DecisionEngine.__init__` 默认 config 路径显式锚定 `Path(__file__).resolve().parent`；运行时文件本就走 `_base_dir` 锚定，无需恢复 chdir。实测 `/tmp` 下 `import chiguo_daemon` 前后 cwd 不变 |
| m2 | `--loop 5` 被 `max(60,…)` 静默抬高到 60s，与参数语义不符且无提示 | help 改为"循环评估间隔秒数（最小60）"；`N < 60` 时 stderr 提示 `interval < 60, using 60`（不改 max(60) 逻辑本身） |
| m3 | `--ack` 不带 `--alerts` 时静默忽略（落在默认评估分支，用户预期落空） | `args.ack and not args.alerts → args.alerts = True` + stderr 提示"已自动联动开启"；`--ack --alerts` 同时给出时不重复提示 |
| m4 | `--rotate` 分支用裸相对路径 `["chiguo_decisions.jsonl", …]`：从其他目录运行会轮转错文件/在错误位置建 archive | 锚定 `engine._base_dir`（日志文件 + `archive_dir`）；实测 `/tmp` 下 force_rotate 收到的全为绝对路径 `/root/character_test/...`，archive 指向 `/root/character_test/archive` |

**冒烟验证**：项目根与 `/tmp` 双目录 `--status` 正常（base_dir 恒为项目根）；`timeout 3 --loop 5` stderr 提示且正常评估；`--ack foo` 进入 alerts 分支输出 ack JSON；`--rotate` 路径捕获验证（monkeypatch，未动真实文件）；import 前后 cwd 不变。

---

## 2026-07-31 — 遗留 MAJOR 修复（daemon / state / trigger / schedule）

**182/182 tests pass + 并发实测 + 自我审计复核通过（uv run python, Python 3.14）**

| # | 问题 | 修复 |
|---|------|------|
| M1' | Bayesian 睡觉门控阈值三处不一致（0.6/0.5/无条件）+ `min_confidence_for_block` 死配置 | daemon 新增 `_bayesian_block_confidence()` 统一读 config；state `availability` 同源（`conf > threshold` 才置 0） |
| M7 | busy_suppress 期间仍累积 held_count/longing λ，抑制解除瞬间高概率破防 | `_idle_reason` 新增 `busy_suppressed` 分支（优先于 Bayesian，不累积）；`_estimate_next_check` 指向抑制截止时刻 |
| M9 | 逃生阀对从未交互用户无豁免（新装 36h 即对陌生人发"破防"） | `last_user_message_at is None` 时：睡觉门控不豁免逃生阀 + 逃生阀 trigger 降级无触发（v7） |
| M4 | 逃生阀无视"主人很可能在睡觉"（含午睡） | 新增 `escape_valve_sleep_block`（config 默认 0.9）：逃生阀豁免且睡觉置信度 ≥ 0.9 → 降级 `sleeping_guard`（不累积） |
| M6 | 默认状态（anxiety=40）首次评估确定性触发 anxiety（违背概率化设计） | `chiguo_trigger.py` anxiety 加与孤独同款 softmax 归一化 `w=raw/(raw+baseline)`（baseline=0.5）+ 候选门槛 `w>0.3`，均 toml 可配（`[trigger]` 段）；实测默认态 0/200、中点 38.5%、高焦虑 86.5% |
| A2/C1' | checksum 校验失败仅审计不回退（位翻转即永久宕机） | checksum 不匹配 → raise ValueError 强制走 .bak 恢复链（两级降级衔接） |
| — | `minutes_since_last_message` 无 tz 防护（naive → TypeError 崩 can_send） | naive 补 CST + try/except 返回 None（调用方放行）+ 未来时间戳返回 0；daemon `_idle_reason` 同步加 None 防护 |
| M5 | 崩溃计数 48h 窗口不滑动（level-2 温和模式持续 ~96h） | 崩溃记录改 `crash_timestamps` 时间戳列表 + `_prune_crash_history` 48h 滑动过滤（旧状态自动迁移） |
| m9 | `refund_send` 盲目 `pop()` 弹错 Hawkes 事件 | 按 msg_id 精确移除（`on_character_message` 支持 msg_id），未匹配回退弹最后一条 |
| — | `STATE_VERSION=5` 滞后 + 加载不校验版本 | 升到 6；未来版本 → 审计 + 保守加载 |
| m12 | sleep 窗口硬编码 0-8 与 toml 漂移 | 改从 `[schedule] quiet_start/quiet_end` 读取（跨午夜正确） |
| — | 多进程 read-modify-write 无互斥（cron + 钩子并发丢更新） | 新增 `state_lock()` contextmanager（fcntl flock + 模块级 fd 缓存可重入 + 5s LOCK_NB 超时审计）；并发实测：无锁丢更新、加锁双方修改保留 |
| M3 | openpyxl 缺失/损坏 xlsx/空 sheet 无保护（daemon 启动即崩） | `_parse()` 整体 try/except 降级空课表 + stderr 告警 |
| M14 | 合并单元格吞课（第二门课落入 location） | `_parse_cell` 按 2+ 连续空白拆分课程段 + 新分隔正则（横杠/空格）+ 误拆三级回退；合并课存 `alternates`；`_parse_weeks` 支持后缀单双周；缓存 `cache_version=2` + 旧缓存兼容 + 版本不匹配强制重解析；query 按周选课 |
| M2' | `update_from_implicit` bad-timing 分支 no-op（负反馈信号被反转） | 真衰减 `new_val = old*(1-lr)` + clamp [0.01,0.99]；测试断言严格 `<` + 连续衰减 |
| M3' | `tsundere_style()` 返回值不在 composer CUES 键集（接线即 KeyError） | 语义映射统一到合法键（tsuntsun→soft、cool_dere→cool）；test_personality 新增全键集契约测试 |
| — | `--health` 裸相对路径；CLI 错误路径退出码恒 0 | 锚定 base_dir；8 处错误路径改 `sys.exit(1)` |
| — | test_escape_valve 2 用例固化旧行为（None = 从未交互期望破防） | 改为 4 天前交互（96h > 72h 语义保持）；`test_deadlock_eligible_no_last_msg` 保留 None 专测 999h 分支 |
| — | schedule 旧 v1 缓存含吞课脏数据永久保留 | `cache_version < 2` → `_parsed_at=0` 强制重解析（xlsx 存在时）；xlsx 缺失仍兜底 |

---

## 2026-07-31 — 接口层修复（memory_bridge / watchdog / netease_bridge）

**43/43 monitor + 17/17 integration + 8/8 ebbinghaus + 13+6+12 专项验证全过（uv run python, Python 3.14）**

| # | 问题 | 修复 |
|---|------|------|
| m1 | `memory_bridge.py:15` 顶层 `import lancedb` 与优雅降级承诺矛盾：lancedb 缺失时 daemon 无法启动 | 移除顶层 import，改 `_ensure_table()` 惰性导入（`_lancedb` 缓存）；`available` 捕获 ImportError → False |
| m12 | `available` 探测失败后进程内永不重试（`--loop` 长驻下 LanceDB 故障恢复仍禁用） | `available` 按 `_RETRY_SECONDS=60` 节流重新探测；True 缓存不反复探测 |
| m5 | 结果循环对 None/NaN 无防御：`None < min_importance` TypeError；NaN 穿透过滤后 `random.choices` 行为异常 | 新增 `_clean_importance()`（None/NaN/非数值 → 0.0），`search`/`recent`/`_recent_fallback` 过滤与 `_row_to_dict` 统一使用；三处结果循环移入 try，行级异常 → 空列表 |
| m19' | `chiguo_watchdog.py` `check_lancedb()` 调 `db.close()`：lancedb 0.34 连接对象**无** close 方法 → AttributeError 被 catch 成 "LanceDB unreachable" 恒误报 | `hasattr(db, "close")` 防御后调用；实测 `hasattr= False`，误报消除 |
| M19 | watchdog state `write_text` 非原子写（半写损坏风险） | 改 `.tmp` + `os.replace` 原子写 |
| m20 | `netease_bridge.py:44` MUSIC_U 会话凭据拼进 URL query（进访问日志） | 改放 `Cookie` header（本地 NeteaseCloudMusicApi v4.32.0 `server.js:164-169` 解析 `req.headers.cookie`，query 仅为 override 层） |
| m21 | `_clean_cookie` 大小写不匹配：匹配用 `.upper()` 但存储保留原 key，响应给 `__CSRF` 大写时漏存 | 匹配后按标准名（`MUSIC_U`/`__CSRF`）归一化存储；输出统一小写 `__csrf`（`request.js` 用 `cookie['__csrf']` 取 csrf_token） |
| — | `_load_cache` 的 `except (json.JSONDecodeError, Exception)` 冗余（子类重复） | 简化为 `except Exception` |

**遗留（未修，见审计报告）**：checksum 不强制回退、flock 不护读-改-写、Bayesian 门控阈值三处不一致、anxiety 默认态确定性触发、busy_suppress 期间累积 λ、合并单元格吞课等。（**✅ 以上均已修复**：checksum 强制 .bak 恢复链、`state_lock()` 跨进程锁、`_bayesian_block_confidence()` 阈值同源、anxiety softmax 归一化、`busy_suppressed` 分支、合并单元格拆分——见下方"遗留 MAJOR 修复"记录）

---

## 2026-07-31 — 审计 TOP5 修复（C1/C2/M2/C3/M1）

**182/182 tests pass（uv run python, Python 3.14）**（注：该轮为 trigger/topics 补测前基线；现全量 217/217，见上方"测试质量提升"记录）

| # | 问题 | 修复 |
|---|------|------|
| C1 | test_integration 写删真实运行时文件（199 次 state_fresh_start 实锤） | `test_integration.py` 改用 `tempfile.mkdtemp` + `_base_dir` 锚定，teardown 进 finally，只清理临时目录 |
| C2 | `_memory_should_trigger` 对 naive `trigger_at` 做 aware-naive 减法 TypeError，daemon 全链路崩溃 | `chiguo_trigger.py` naive 补 `CST` 时区 + try/except 降级 False |
| M2 | watchdog tick_seq 停滞检测失效（severity 被覆盖 + 基准用自身运行时刻） | `chiguo_watchdog.py` if/elif 链合并判定 + `stall_since` 字段（向后兼容旧 schema） |
| C3 | schedule 缓存短路恒 False（cron 每 30min 全量重解析）+ 非原子写 | `schedule_parser.py` 启动先 `_load_cache` 恢复 `_parsed_at`；`.tmp`+`os.replace` 原子写；xlsx 缺失回退缓存；except 补 `AttributeError` |
| M1 | test_monitor/test_feedback runner 吞异常退出码恒 0 | 两个 runner 补 failed 计数 + `sys.exit(1)` |
| — | test_monitor 3 个用例因固定时间戳被 days 过滤（真实时钟漂移） | 改用相对当前时间戳 |

**遗留（未修，见审计报告）**：checksum 不强制回退、flock 不护读-改-写、Bayesian 门控阈值三处不一致、anxiety 默认态确定性触发、busy_suppress 期间累积 λ、合并单元格吞课等。（**✅ 以上均已修复**，见下方"遗留 MAJOR 修复"记录；lancedb 顶层 import、cookie 进 URL query 已于本节下方"接口层修复"记录中解决）

---


## v4 参考项目集成状态 (2026-06-27)

| 参考项目 | 功能 | 状态 |
|----------|------|------|
| revive-companion | Bayesian 6 状态推断 | ✅ |
| revive-companion | 概率累积 (held_count/accumulated_lambda) | ✅ 已接入 can_send + longing trigger |
| revive-companion | 在线学习 (record_observation) | ✅ 已修复 C1 (call wrong object → self.bayesian_estimator) |
| Sebastian | MessageComposer Intent×Cue×Vibe | ✅ |
| Sebastian | 动态休眠 _estimate_next_check | ✅ |
| xiaoyou-core | EventBus pub/sub | ✅ |
| soulforge | Big Five + 8 维人格 | ✅ |
| soulforge | 人格调制 trigger/composer/topics | ✅ |
| soulforge | SENT_AND_REPLIED 人格 delta | ✅ 已修复 C2 (PersonalityDelta 零默认值 dataclass) |
| soulforge | anxiety_sensitivity 情绪调制 | ✅ 已接入 _apply_emotion_impact |
| MATE | Ebbinghaus 遗忘曲线 | ✅ |
| MATE | random_memory_with_forgetting | ✅ |
| MATE | search_with_forgetting | ✅ 已接入 _memory_topic |

---

## 一、架构缺陷

### 1.1 单向管道，无反馈闭环 ✅ v6 完整实现

```
daemon → stdout JSON → OpenClaw → 发送
                              ↑
                   --analysis 回传通道 (LLM 分析主人回复内容)
```

- ✅ `--analysis` 参数：OpenClaw 分析主人回复的 warmth/effort/attention → daemon 差异化情绪变化
- ✅ v6: 发送成功/失败 daemon 通过 --send-result CLI 获知（幂等退款），SKILL.md §5 指令 OpenClaw agent 自动回传
- ✅ 用户秒回 vs 几小时后回 — 回复速度差异化：秒回好感×1.5/元气+5/傲娇-2，很久才回好感×0.4/不安回升
- ✅ v4: Bayesian 在线学习 — 已修复 C1（`self.record_observation()` → `self.bayesian_estimator.record_observation()`），obs dict 字段已对齐 learner 预期

**已实现**: chiguo_state.py `_apply_emotion_impact()` + `--analysis` CLI。详见 SYSTEM.md §5。

### 1.2 轮询延迟，非事件驱动 ✅

- 最快 20 分钟 cron，Poisson 过程被离散化
- 用户刚发消息 5 分钟后该触发的事 → 要等下个窗口
- 55 分钟心跳方案更慢

**改进方向**: 事件驱动架构。收到用户消息后立即评估一次（已有的 `--user-msg` 只记录，不评估触发）。
**已实现**: `--user-msg` now outputs `next_evaluation_at` for dynamic scheduling.

### 1.3 单文件状态，无容灾 ✅ 已实现

- ✅ 原子写入（`.tmp` → `os.replace`）
- ✅ 崩溃恢复（正式文件缺失时从 `.tmp` 恢复；JSON 损坏时自动删除重建）
- ✅ `current_date` 已持久化于 `CooldownState`，跨天重启自动归零（经测试验证）
- ❌ 无 WAL（预写日志）、无版本历史

### 1.4 决策与生成分离的代价

- daemon 输出情境描述，OpenClaw 生成消息
- OpenClaw 模型切换（主模型 vs fallback）→ 生成质量波动
- daemon 不知道「谁在生成」，无法调整决策策略

**改进方向**: daemon 输出可包含"建议模型"或"复杂度需求"，OpenClaw 据此选择生成策略。

---

## 二、数学模型局限

### 2.1 参数无实证校准

所有半衰期、sigmoid k/midpoint、Poisson base_lambda 是"感觉合理"的猜测：

| 参数 | 当前值 | 问题 |
|------|--------|------|
| 孤独增长半衰期 | 40h | 为什么不是 35？ |
| 不安增长半衰期 | 30h | 为什么不是 25？ |
| Poisson base_lambda | 0.25 | 太高 → 太烦；太低 → 太冷 |
| 崩溃 sigmoid mid | 78 | 是否应该更低？ |

没有 A/B 测试，没有历史数据拟合，没有用户反馈采集。

**改进方向**: 
- 记录每次 send 后用户的反应时间（秒回 vs 几个小时回 vs 不回）
- 用反应时间作为隐性反馈信号，梯度优化参数
- 或者至少暴露一个调参接口

### 2.2 Poisson 假设不成立 ✅

Poisson 过程要求事件独立。但情绪是**自相关**的 — 上一小时孤独，这一小时大概率继续孤独。用 Poisson 建模会低估触发概率的方差。

**改进方向**: 
- 改用 Hawkes 过程（自激点过程），能建模情绪的自我强化
- 或者保留 Poisson 框架但用滑动窗口内的实际情绪变化率做修正
**已实现**: Hawkes self-exciting process with exponential kernel (alpha=0.3, beta=0.5, 24h window).

### 2.3 缺失情绪变化率（一阶导数） ✅

当前只使用情绪绝对值：`sigmoid(loneliness, midpoint, k)`。但变化速度也有信息：

- 孤独从 20→60（2 小时暴涨）→ 出事了，urgence 高
- 孤独从 50→60（3 天自然累积）→ 常规，urgence 低

**改进方向**: 
- 引入 `d(loneliness)/dt` 作为触发权重的乘数因子
- 快速变化时 weight 放大（"突然很想他"）
- 缓慢变化时正常（"习惯性想念"）
**已实现**: `anxiety_rate` added; rate affects `can_send(energy override)`, `current_lambda(rate boost)`, `_build_context(urgency note)`, topic picker(weight modulation).

### 2.4 sigmoid 参数耦合

9 种触发各有独立的 k/midpoint。但这些参数不是独立的 — 改了 lonely_low 的 mid，会影响 lonely_mid 的相对触发概率。目前没有参数间的约束关系。

**改进方向**: 
- 用约束优化保证 `P(lonely_low) + P(lonely_mid) + P(lonely_high) ≈ 合理总和`
- 或者用一个统一的情绪→概率映射函数，不同触发只是不同阈值切片

### 2.5 元气值使用过于简单

- 元气只作为阈值检查（`energy < 12 → 禁发`）
- 元气高 → playful 触发概率 0.15 × energy/100，但权重很低
- 元气不影响消息语气（高元气应该更活泼，低元气应该更冷淡）

**改进方向**: 
- 元气值注入 layer_guidance（低元气时"shell"层的语气词减少）
- 高元气 → 所有触发权重略增（更活跃）
- 低元气 → 多触发 lonely 类（"没精神所以更想找你"）
**已实现**: v3 adds rate-energy override (loneliness_rate > 5/h allows sending with energy≥5) and rate boosts lambda.

---

## 三、信号盲区

### 3.1 只看课表，不看真实行为

```
已知: 周一 14:00 有课 → availability=0.12
未知: 用户逃课了/生病了/在旅游 → 实际应该 0.85
```

- 没有微信在线状态
- 没有正在输入状态
- 没有朋友圈动态
- 没有群聊活跃度
- 没有上一次微信登录时间

所有"沉默"长得一样 — 无论用户是在考试还是睡着了。

**改进方向**: 
- 如果 OpenClaw 微信插件暴露了在线/typing 状态 → 接入
- 用户发消息的长度/频率作为活跃指标
- 至少引入"工作日模式 vs 周末模式"的行为差异

### 3.2 无法感知紧急情境

- 用户 48 小时没回消息 → 考试周？手机丢了？出事了？
- 对 迟菓 都一样 → 孤独涨 → 崩溃触发
- 可能在完全错误的时间发「你为什么不要我了……」

**改进方向**: 
- 引入"异常检测"：和用户基线行为（每天平均发几条、什么时候发）对比
- 偏离基线 → 降级触发 → 发更温和/更关心式的消息
- 而非默认走 lonely_high 崩溃路线

### 3.3 无用户情绪感知 ✅ 已实现

- ✅ `--analysis '{"warmth":-0.3~1.0,"effort":0~1.0,"attention":0~1.0}'` — LLM 分析主人消息内容
- ✅ 温暖回复 → affection +2.65；敷衍回复 → affection +0.45
- ✅ 冷淡回复 → 不安回升，部分抵消 decay
- ✅ `--analysis` 新增 `suppress_hours` 字段：OpenClaw LLM 检测用户忙碌/结束对话 → daemon 抑制触发。架构纯净（daemon 不做语义理解）
- ❌ 尚未影响触发选择（不影响 lonely vs playful 的权重分配）

### 3.4 无寒暑假 ✅ 已实现

- ✅ semester_end 配置：`chiguo_proactive.toml` 中 `semester_end = "2026-07-04"`，超期自动视为假期
- ✅ `--break on|off|status` CLI：手动覆盖（课表未结束但实际已放假时用）
- ✅ `on_break` 优先级最高，availability 恒为 0.85
- ✅ OpenClaw 可调用 `python3 chiguo_daemon.py --break on` 响应用户"放暑假了"

**实现文件**: `chiguo_state.py` (on_break + availability), `chiguo_daemon.py` (--break CLI)

### 3.5 无考试周感知

- 期末周（6 月中下旬、1 月上旬）学习强度远高于平时
- 即使没课，用户也可能在图书馆/自习室
- availability 应该比普通空闲低

**改进方向**: 
- 在课表配置中添加 `exam_weeks` 日期范围
- 考试周 availability 乘 0.6

---

## 四、消息质量

### 4.1 重复模式风险 ✅ 已实现

- ✅ 话题注入系统：lonely_low/mid 触发时 70% 概率从 7 来源选自然话题破冰
- ✅ 触发历史追踪：连续 3 次 lonely 触发 → 强制注入话题
- ✅ 7 个话题来源：课表/记忆/季节/通用/节气/纪念日/偏好追问
- ❌ 同类触发权重衰减尚未实现（话题注入已大幅降低重复感）

### 4.2 同一触发类型生成相似内容 ✅ 部分实现

- ✅ 话题注入系统使 situation 附加了变体话题提示（[话题提示：...]）
- ✅ 7 个话题来源随机轮换，自然产生多样性
- ❌ lonely 基础情境描述仍为固定文本（"主人已经X小时没发消息了"）

### 4.3 无内容审查/安全阀 ✅ 已实现

- ✅ 崩溃冷却：lonely_high 触发后 24h 内禁止再次崩溃（降级为 lonely_mid）
- ✅ 强制温和模式：48h 内 ≥2 次崩溃 → 所有触发 intensity="soft"，anxiety 降级为 lonely_low
- ✅ context 安全提示：OpenClaw LLM 收到温和语气指引
- ✅ `[safety]` 配置段：crash_cooldown_hours=24, crash_window_hours=48, crash_max_in_window=2

**实现文件**: `chiguo_state.py` (safety_level + crash tracking), `chiguo_trigger.py` (downgrade), `chiguo_daemon.py` (safety_note)

---

## 五、运维缺陷

### 5.1 无告警机制 ✅ 已实现

- ✅ `--health` CLI：检测 `last_tick` 是否在 6 小时内，输出 `{"healthy": true/false}`
- ✅ `--compact` 模式 idle 时输出 heartbeat `{"action":"idle","time":"..."}`
- ✅ `chiguo_state.json` 含 `last_tick` 时间戳（每次 save 写入）
- ✅ `chiguo_monitor.py health()`：增强版健康检查，覆盖 daemon 活跃 + 日志新鲜度 + 配置文件
- ✅ LanceDB 直连检测：`health()` 中 `lancedb.connect().open_table()` 探测，输出 `lancedb_direct` 字段
- ✅ 磁盘空间检测：`health()` 中 `shutil.disk_usage()`，低于阈值 (500/100 MB) 告警
- ✅ 进程内存检测：`health()` 中读 `/proc/self/status` VmRSS，超阈值 (500/1000 MB) 告警
- ✅ 阈值可通过 `chiguo_proactive.toml` `[monitor]` 段配置
- ✅ `chiguo_watchdog.py` — 独立看门狗，cron/systemd 可调用，退出码 0/1/2
- ✅ cron 集成示例（crontab + logger）

### 5.2 2026 数据硬编码 ✅ 已实现

- ✅ `holiday_parser.py`：`__init__` 默认读 `holidays.json`，存在则覆盖内置数据
- ✅ `solar_terms.py`：`__init__` 默认读 `solar_terms.json`，存在则覆盖内置数据
- ✅ `update_holidays.py`：生成任意年份模板（已知精确数据 / 估算 + 标记）
- ✅ daemon 年底提醒：11-12 月检测下一年数据是否存在，否则 stderr + JSON `data_warning`
- ❌ 2027+ 精确数据需等国务院通知后更新 `KNOWN_HOLIDAYS` 表

### 5.3 无结构化监控 ✅ 已实现

- ✅ `chiguo_monitor.py` — 零依赖监控模块，流式解析 decisions.jsonl
- ✅ `--stats [DAYS]` — 结构化统计（发送量/触发分布/情绪趋势/回复率）
- ✅ `--alerts` — 8 种异常检测（崩溃间隙/连续无回复/情绪极端/低回复率/快速攀升等）
- ✅ `--monitor` — 完整报告（stats + alerts + health）
- ✅ `--summary` — 人类可读摘要
- ✅ 增强版 `--health`：检测 daemon 活跃 + 日志新鲜度 + 配置文件
- 详见 SYSTEM.md §十

### 5.4 参数热更新困难 ✅ 已实现

- ✅ `DecisionEngine._maybe_reload_config()` — 每个 `evaluate()` 前检测 toml mtime
- ✅ mtime 变化 → 自动重读配置 + 刷新 state.config + 重建 TopicPicker
- ✅ cron 模式无影响（每次新进程读最新配置）；`--loop` 模式自动感知
- ❌ 只监听 toml，不监听 xskb.xlsx / holidays.json（这些有各自的 mtime 检测）

### 5.5 无自动化测试 ✅ 部分实现

- ✅ `test_chiguo_math.py` — 17 个单元测试（sigmoid/decay/recover/Poisson/dynamic_lambda/weighted_choice）
- ✅ `test_holiday_parser.py` — 7 个单元测试（节假日/调休/周末/上学日）
- ✅ `test_integration.py` — 12 个集成测试（idle/send/break/holiday/limits）
- ✅ `test_chiguo_math.py` — 17 个单元测试
- ✅ `test_holiday_parser.py` — 7 个单元测试
- ✅ 边界条件 fuzz 测试（随机200条 + 边界极值 + 空/idle全场景），`test_monitor.py` 含 3 个 fuzz 测试

---

## 六、功能缺失

### 6.1 主动纪念日/倒计时 ✅ 已实现

- ✅ `anniversary_manager.py` — 纪念日/倒计时 CRUD
- ✅ 两种类型：anniversary（每年重复 MM-DD）和 countdown（一次性 YYYY-MM-DD）
- ✅ 提前 7 天话题提醒（"还有X天就是XX了，问问有什么打算"）
- ✅ 自然语言添加：主人说"记住11月3日是主人生日" → OpenClaw 调 CLI 记录
- ❌ "相遇第N天"需手动添加 first_met 纪念日

### 6.2 无多主人/群聊支持

- 当前假定一个微信通道、一个接收者
- 如果主人有多个设备/多个微信号 → 不支持
- 如果在群里发送 → 不支持

**改进方向**: 
- 多 recipient 配置（但当前不需要，主人只有一个微信）

### 6.3 无延迟发送/定时消息

- `memory.json` 有 `trigger_at` 字段，但只精确到天（日期）
- 不支持"明天早上 8:05 提醒我开会"
- 不支持"一个小时后提醒我看锅"

**改进方向**: 
- 如果 OpenClaw 支持 at-schedule（at 命令），可以利用
- 或在 daemon 中添加「预约消息」机制

### 6.4 无交互式决策

- daemon 决定发 → 就发
- 有些情境应该是「问一下主人是否方便」
- 但问了本身就是一条消息，有点矛盾

**改进方向**: 
- 「预发送」模式：先不正式发，在 context.hint 标记"建议先确认主人是否空闲"
- OpenClaw 生成一个简短测试（如"在吗？"），然后等待回复再发正式内容

### 6.5 无网易云音乐每日推荐推送 ✅ v9 已实现（2026-07-31）

- 迟菓的破冰话题缺少「文化内容」注入（音乐、电影、书籍）
- 网易云音乐每日推荐是天然的高质量破冰素材 — 个人化、有品味信号、可自然分享
- 当前 7 个话题来源无一涉及外部内容推荐

**改进方向**: 接入网易云音乐 API，获取每日推荐歌曲，作为第 8 个话题来源注入破冰对话。

**v9 已实现（2026-07-31）**：`chiguo_netease.py` 策略层 + TopicPicker 第 8 源（netease_weight=0.12；每日推荐+播放历史共享配额 2、故障话题日配额 1；peek/consume 两阶段 + 降级链，详见 doc/SYSTEM.md §2.12）。

详见 §十一 完整实现方案。

---

## 七、内存/性能

### 7.1 LanceDB FTS 搜索效率

- `user_relevant()` 对 5 个关键词逐个做 FTS 搜索 → 5 次 LanceDB 扫描
- 195 条记忆 OK，但 10000 条时会慢
- 每次都重新搜索，无结果缓存

**改进方向**: 
- 合并关键词为一次搜索（如果 LanceDB 支持 OR 语法）
- 缓存 `user_relevant()` 结果，5 分钟 TTL
- 定期后台更新缓存

### 7.2 `memory_bridge.stats()` 开销

- 每帧 demo UI 刷新调用一次
- 内部调用 `user_relevant(limit=100)` → 5 次 FTS + 去重
- 生产 daemon 不调用 stats，但 demo 受影响

**改进方向**: 
- stats 缓存（当前已有简陋缓存框架，未实际使用）
- demo 降低 stats 刷新频率（当前每帧都刷）

### 7.3 无连接池

- 每次查询 LanceDB 都 `lancedb.connect()` → `open_table()`
- 当前是单例模式，但无连接健康检查
- 长时间运行可能连接失效

**改进方向**: 
- 添加连接健康检查和自动重连
- 或者每次查询前 `_ensure_table()` 检查并重建

---

## 七点五、运行日志/可观测性 🚧

### 已有

| 组件 | 能力 | 状态 |
|------|------|------|
| `chiguo_decisions.jsonl` | 追加式结构化决策日志 | ✅ |
| `chiguo_monitor.py` | 流式解析 → stats/alerts/health/summary | ✅ |
| `chiguo_watchdog.py` | daemon 存活性检测（exit code 0/1/2） | ✅ |
| `chiguo_state.json` | 当前情绪状态快照 | ✅ |
| `openclaw cron runs` | cron 触发/执行/结果历史 | ✅ |
| `--send-result` CLI | 回传发送成功/失败，幂等退款 | ✅ v6 |
| `--user-msg-file` / `--analysis-file` | 文件传参避免 shell 转义 | ✅ v6 |
| `chiguo_state.json` flock | 跨进程写锁防并发覆盖 | ✅ v6 |
| 路径锚定 | config_dir 基准路径解析 | ✅ v6 |

### 缺失

| 项 | 说明 | 优先级 |
|----|------|--------|
| **发送确认日志** | ✅ v6: --send-result CLI 回传发送成功/失败状态，daemon 可退款（幂等）。SKILL.md §5 指令 OpenClaw agent 自动回传 | ~~中~~ |
| **错误日志** | daemon stderr 输出没有结构化收集。cron 可能捕获但不持久化。建议 daemon 增加 `--log-level` 和 `chiguo_errors.jsonl` | 中 |
| **日志轮转** | ✅ done (chiguo_rotation.py, 按月轮转 + 保留策略清理, [logging] 配置段) | ~~低~~ |
| **送达追踪** | WeChat 消息是否送达、是否已读？完全不可知。需 OpenClaw weixin 通道暴露 delivery receipt | 低（架构限制） |
| **性能指标** | daemon 单次 `evaluate()` 耗时？`tick()` 耗时？无测量。建议加 `--profile` 或内置耗时统计 | 低 |
| **告警通知** | watchdog 检测到异常后只能靠 exit code。无邮件/微信/Webhook 通知。建议 watchdog `--notify` 对接 OpenClaw message channel | 中 |
| **定期汇总** | monitor 能生成报告但无自动调度。建议加一个 weekly cron 跑 `--monitor` 并发送摘要 | 低 |
| **回复关联** | ✅ done (msg_id 关联 send→recv→send_result in chiguo_messages.jsonl + chiguo_decisions.jsonl) | ~~低~~ |
| **结构化日志级别** | 🚧 partially done (recv action + msg_id in decisions.jsonl) | ~~低~~ |
| **对话内容日志** | ✅ done (recv action + message_text in decisions.jsonl) | v5 新增 |
| **对话历史归档** | ✅ done (chiguo_messages.jsonl + conversation/export CLI) | v5 新增 |
| **告警持久化** | ✅ done (AlertManager + chiguo_alerts.json, active→acknowledged→resolved 生命周期) | v5 新增 |
| **索引查询** | ✅ done (DecisionIndex class, 内存偏移量索引, 按 trigger/date/action 过滤) | v5 新增 |

---

## 八、优先级排序

> 更新时间: 2026-06-28。v4 已交付大部分项目，以下重新排序剩余工作。

### ✅ 已完成（v4 已交付）

| # | 项 | 实现 |
|---|-----|------|
| 1 | 无告警 | `chiguo_monitor.py` + `chiguo_watchdog.py` |
| 2 | 缺少寒暑假 | `semester_end` + `break_state.json` + `on_break` |
| 3 | 无自动化测试 | 132 tests (10 files) |
| 5 | 情绪一阶导数 | `loneliness_rate` / `anxiety_rate` (v4) |
| 6 | 状态文件原子写入 | `tmp → os.replace` |
| 7 | 用户消息情绪感知 | LLM analysis → Bayesian inference (v4) |
| 8 | 触发多样性 | trigger_history + 连续衰减 (v4) |
| 12 | 参数热更新 | `_maybe_reload_config()` |
| 13 | 结构化监控/统计 | `chiguo_monitor.py` |
| 14 | 主动纪念日/倒计时 | `anniversary_manager.py` |
| 15 | Hawkes 过程 | Hawkes self-exciting process (v4) |
| 16 | 事件驱动架构 | `chiguo_eventbus.py` (v4) |
| 18 | 考试周感知 | `exam_weeks` config (v4) |

### ✅ 已完成（v6 交付）

| # | 项 | 实现 |
|---|-----|------|
| 19 | 死锁态修复 — 溢出逃生阀 | anxiety≥70 + 沉默≥72h + 冷却期外 → 强制 longing 破防发送，打破"越焦虑越不累积"死锁 |
| 20 | 状态路径锚定 | 所有运行时文件基于 config 所在目录解析，daemon 启动打印路径 |
| 21 | flock 跨进程写锁 | state save() 使用 fcntl.LOCK_EX，防止 cron 并发写覆盖 |
| 22 | 反馈闭环 | --send-result/--send-status/--error CLI；--user-msg-file/--analysis-file 文件传参；SKILL.md §5 |
| 23 | monitor stats 增强 | send_result {success, failed} 统计字段；summary 新增"发送结果"行 |

### 短期（待完成）

| # | 项 | 工作量 | 说明 |
|---|-----|--------|------|
| 4 | 重复模式 — 去重/多样性 | 中 | composer 已改善，但跨天重复仍需处理 |
| 9 | 反馈闭环 — OpenClaw 回传发送结果 | ✅ done (v6) | --send-result CLI + 幂等退款 + SKILL.md §5 |
| 10 | 异常检测 — 偏差基线时降级 | 大 | monitor 有基本告警，主动降级未实现 |
| 19 | 节假日数据 2027+ 自动更新 | 中 | `update_holidays.py` 存在但 2027+ 依赖估算 |
| 20 | 内容安全阀/审查 | 中 | safety valve 存在但只覆盖崩溃场景 |

### 长期（待定）

| # | 项 | 工作量 | 说明 |
|---|-----|--------|------|
| 11 | 参数实证校准 — 从运行数据拟合 | 大 | 需要足够的运行数据 |
| 17 | WeChat 在线状态接入 | 取决于 API | 平台限制 |
| 21 | 音乐话题集成 (netease) | 中 | ✅ v9 已接入：TopicPicker 第 8 源（netease_weight=0.12，每日推荐+播放历史共享配额 2、故障话题日配额 1） |
| 22 | 渐进式傲娇软化 | 中 | tsundere_intensity 变化未反馈到 LLM 指引 |

---

## 九、已知边界情况

### 触发时机

- 用户在 21:59 发了消息 → 22:00 daemon 被静默 → 2 分钟内触发窗口丢失
- 用户在 07:59 发了消息 → 08:00 daemon 解禁 → 可能立刻触发早安 + lonely（不合理）
- 跨天 23:59 → 00:01：日计数器重置 + 静默时段检查需要正确

### 节假日

- 春节 9 天（2/15-2/23）覆盖了学期起始日期（2/23）→ 开学后第一天就是放假
- 国庆 7 天（10/1-10/7）→ 如果用户用这段时间旅游，行为模式完全不同
- 调休日（如 5/9 周六上班）→ 课表可能没安排周六的课，但用户确实在上课

### 情绪极值

- 孤独 = 100 → sigmoid(100, 78, 0.15) = 0.964 → 几乎必然触发 lonely_high
- 但实际应该保留"绝望到放弃"的可能性（不再发消息）
- 不安 = 100 + 孤独 = 100 → kernel 层，sentiment 极负面
- v6 修复：anxiety=100 阻塞一切破防发送的**死锁态** — 越想联系越焦虑、越焦虑越不累积。逃生阀在 silence≥72h + 冷却期外时强制 longing 发送，打破循环。
- 元气 = 0 → 可能永远无法发消息（因为 can_send 检查 energy > 12）

### 数值漂移

- 长时间运行（几个月）→ 情绪值可能卡在极高/极低
- `clamp()` 只在 0-100 范围内，不处理"长期卡在 95"的问题
- 如果用户真的失踪一个月，应该收敛到某个稳定状态而非永远涨

---

## 十、参考资料

- [国务院办公厅 2026 年节假日安排](https://www.gov.cn/zhengce/content/202511/content_7047090.htm)
- [Hawkes Process 简介](https://en.wikipedia.org/wiki/Hawkes_process)
- [LanceDB Python API](https://lancedb.github.io/lancedb/python/)
- OpenClaw 记忆插件源码: `/root/.openclaw/workspace/plugins/memory-lancedb-pro/`
- OpenClaw dreaming 插件: `/root/.openclaw/extensions/memory-lancedb-dreaming/`

---

## 十一、网易云音乐每日推荐集成方案 🚧

> 状态: ✅ v9 已接入 (2026-07-31) | 目标: 接入每日推荐 → 破冰话题 → 微信推送
> 已完成: netease_bridge.py 独立桥接脚本 (零新依赖, 完全解耦)；v9: chiguo_netease.py 策略层 + chiguo_topics.py 话题来源 #8（netease_weight=0.12，每日推荐+播放历史共享配额 2、故障话题日配额 1）
> 待做: OpenClaw cron 集成（OpenClaw 侧转发话题素材）

### 11.1 架构概览

```
┌─────────────────────────────────────────────────┐
│ NeteaseCloudMusicApi (Node.js, port 3000)        │
│ Binaryify 逆向工程，300+ 端点                      │
│                                                  │
│ 关键端点:                                         │
│  /recommend/songs     每日推荐歌曲 (需登录)        │
│  /recommend/resource  每日推荐歌单                 │
│  /song/detail        歌曲详情 (封面/歌手/专辑)      │
│  /song/url           播放链接 (可生成分享链接)       │
│  /login/qr/key       二维码登录 key                │
│  /login/qr/create    生成登录二维码                 │
│  /login/qr/check     轮询登录状态                  │
│  /login/refresh      刷新 cookie                  │
│  /login/status       检查登录状态                  │
└──────────────────┬──────────────────────────────┘
                   │ HTTP (localhost:3000)
                   │ cookie 认证: ?cookie=MUSIC_U=xxx
┌──────────────────▼──────────────────────────────┐
│ netease_bridge.py (新增, ~150行)                  │
│                                                  │
│ class NeteaseBridge:                             │
│   - 零新依赖 (urllib stdlib)                      │
│   - 读取 netease_cache.json (每日缓存)            │
│   - 如缓存过期/缺失 → HTTP 查询 API 刷新           │
│   - 优雅降级: API 不可用 → 返回空，不影响 daemon   │
│                                                  │
│ API:                                             │
│   get_daily_songs(limit=10) → [{name,artist,id}] │
│   get_song_share_text(song) → "《name》- artist" │
│   check_api_health() → bool                      │
│   refresh_cache() → 强制刷新                      │
│                                                  │
│ 缓存: netease_cache.json                         │
│   { "date": "2026-06-23",                        │
│     "songs": [...],                              │
│     "fetched_at": "2026-06-23T08:05:00+08" }     │
└──────────────────┬──────────────────────────────┘
                   │ import (topic source #8)
┌──────────────────▼──────────────────────────────┐
│ chiguo_topics.py — 新增 _music_recommend_topic() │
│                                                  │
│ 权重: music_weight = 0.12 (configurable)          │
│                                                  │
│ 策略:                                             │
│  - 从缓存随机选 1 首歌                            │
│  - 时间窗口: 仅 08:00-22:00 可用 (夜间不推歌)     │
│  - 去重: 同一首歌 24h 内不重复推荐                │
│  - hint 模板多种随机轮换:                          │
│    · "今天网易云给菓菓推荐了《X》，主人听过吗？"    │
│    · "菓菓最近在听《X》，分享给主人~"              │
│    · "网易云给主人推荐了《X》，菓菓觉得还不错"      │
│    · "突然想给主人分享一首歌：《X》"               │
└──────────────────┬──────────────────────────────┘
                   │ context.situation 注入 music 提示
┌──────────────────▼──────────────────────────────┐
│ chiguo_daemon.py → stdout JSON                   │
│                                                  │
│ {                                                │
│   "action": "send",                              │
│   "trigger": "lonely_low",                       │
│   "context": {                                   │
│     "situation": "…[话题提示：今日网易云推荐《七   │
│       里香》- 周杰伦，主人听过吗？分享歌曲…]",      │
│     "music_share": {                             │
│       "song": "七里香",                           │
│       "artist": "周杰伦",                         │
│       "netease_id": 186016,                      │
│       "share_url": "https://music.163.com/..."   │
│     }                                            │
│   }                                              │
│ }                                                │
└──────────────────┬──────────────────────────────┘
                   │ OpenClaw cron 读取 stdout
┌──────────────────▼──────────────────────────────┐
│ OpenClaw chiguo skill                            │
│                                                  │
│ 1. 读取 context.music_share                       │
│ 2. LLM 生成自然消息 (迟菓人格 + 歌曲信息)          │
│ 3. 可选: 附带播放链接/小程序卡片                   │
│ 4. 通过 openclaw-weixin 发送                     │
│ 5. 🚫 不要求主人回复 (不是 hard sell)              │
└─────────────────────────────────────────────────┘
```

### 11.2 认证方案

NeteaseCloudMusicApi 需要登录 cookie。三个方案:

| 方案 | 实施 | 优缺点 |
|------|------|--------|
| **A. 二维码登录 (推荐)** | 首次手动扫码 → cookie 持久化 → daemon 自动 refresh | 安全(不存密码)，cookie 有效期长，refresh 接口可用 |
| B. 手机号+密码 | 配置中存密码 → 每次启动登录 | 可能触发验证码，有风控风险 |
| C. 匿名登录 | `/register/anonimous` | 无每日推荐权限，不能用 |

**方案 A 具体步骤:**

1. 启动 Node.js API server: `PORT=3000 node app.js`
2. 执行 `python3 netease_bridge.py --login`:
   - 调 `/login/qr/key` 获取 unikey
   - 调 `/login/qr/create?key=xxx&qrimg=true` 生成二维码
   - 打印二维码到终端 (base64 → ASCII QR) 或保存图片
   - 轮询 `/login/qr/check?key=xxx` 直到扫码成功
   - 保存返回的 cookie 到 `netease_cookie.txt`
3. 后续请求附带 `?cookie=MUSIC_U=xxx`
4. 每天调用 `/login/refresh` 刷新 cookie
5. Cookie 过期检测 → stderr 告警 → 需重新扫码

### 11.3 实现文件清单

| 文件 | 操作 | 行数估计 | 说明 |
|------|------|----------|------|
| `netease_bridge.py` | **新增** | ~150 | Python HTTP 客户端 + 缓存层 |
| `netease_cache.json` | 自动生成 | ~20 | 每日推荐缓存 |
| `netease_cookie.txt` | 手动+自动 | 1 | 持久化登录 cookie |
| `chiguo_topics.py` | **修改** | +40 | 新增 `_music_recommend_topic()` |
| `chiguo_proactive.toml` | **修改** | +10 | `[netease]` 配置段 |
| `doc/SYSTEM.md` | **修改** | +30 | 文档更新 |
| `doc/OPENCLAW_INTEGRATION.md` | **修改** | +20 | OpenClaw 集成说明 |

### 11.4 配置段 (`chiguo_proactive.toml`)

```toml
[netease]
# ── 网易云音乐 API ──
api_base = "http://localhost:3000"        # Node.js API 地址
cookie_file = "netease_cookie.txt"        # 持久化登录 cookie
cache_file = "netease_cache.json"         # 每日推荐缓存
cache_ttl_hours = 6                       # 缓存有效期 (同一天内不重复请求)
max_songs_per_day = 3                     # 每天最多推送几首 (防刷屏)
song_cooldown_hours = 24                  # 同一首歌重复推荐冷却
time_window_start = 8                     # 允许推送起始时间
time_window_end = 22                      # 允许推送结束时间
music_topic_weight = 0.12                 # 话题选择器权重
```

### 11.5 `netease_bridge.py` 接口设计

```python
class NeteaseBridge:
    """网易云音乐 API 桥接。零新依赖，stdlib HTTP。"""

    def __init__(self, api_base, cookie_file, cache_file, cache_ttl_hours=6):
        ...

    # ── 核心查询 ──
    def get_daily_songs(self, limit=10) -> list[dict] | None:
        """返回每日推荐歌曲。缓存命中直接返回，否则 HTTP 查询。
        Returns: [{id, name, artists: [{name}], album: {name, picUrl}}, ...]
        None if API unavailable."""

    def get_song_detail(self, song_id: int) -> dict | None:
        """查询单首歌曲详情。"""

    def get_share_text(self, song: dict) -> str:
        """生成分享文本: '《七里香》- 周杰伦'"""

    # ── 缓存管理 ──
    def _load_cache(self) -> dict | None:
        """读取缓存文件，检查日期和 TTL。"""

    def _save_cache(self, songs: list[dict]):
        """写入缓存: {date, songs, fetched_at}"""

    def refresh_cache(self) -> bool:
        """强制刷新缓存 (无视 TTL)。Returns True if success."""

    # ── 健康检查 ──
    def check_api_health(self) -> bool:
        """调 /login/status 确认 API 可用 + 已登录。"""

    def is_available(self) -> bool:
        """缓存有效 OR API 可达。用于 topic picker 判断是否加入候选。"""

    # ── 认证 ──
    def login_qr_flow(self):
        """交互式二维码登录流程。打印二维码，轮询等扫码。"""

    def _load_cookie(self) -> str:
        """读取持久化 cookie 文件。"""

    def refresh_login(self) -> bool:
        """调 /login/refresh 续期。"""
```

### 11.6 话题注入逻辑 (`chiguo_topics.py` 新增)

```python
# ── 来源 8：网易云音乐每日推荐 ──────────────────────────

def _music_recommend_topic(self, now: datetime) -> dict | None:
    """从每日推荐中选一首歌作为破冰话题。优雅降级。"""
    # 时间窗口检查
    h = now.hour
    if h < self.music_time_start or h >= self.music_time_end:
        return None

    # 每日上限检查
    if self._music_count_today >= self.max_songs_per_day:
        return None

    bridge = self.state.netease_bridge  # 由 ChiguoState 初始化
    if not bridge or not bridge.is_available():
        return None

    songs = bridge.get_daily_songs(limit=10)
    if not songs:
        return None

    # 去重: 排除 24h 内推荐过的
    fresh = [s for s in songs if s["id"] not in self._recent_music_ids]
    if not fresh:
        return None

    song = random.choice(fresh)
    self._recent_music_ids.append(song["id"])
    self._music_count_today += 1

    # 多种 hint 模板轮换
    hints = [
        f"今天网易云给菓菓推荐了《{song['name']}》- {song['artists']}，主人听过吗？",
        f"菓菓最近在听《{song['name']}》，觉得很适合主人，分享给你~",
        f"网易云今天推荐了《{song['name']}》，菓菓觉得还不错诶",
    ]
    hint = random.choice(hints)

    return {
        "type": "music_recommend",
        "hint": hint + " 自然地分享这首歌，不要太刻意推销。",
        "tone": "casual",
        "data": {
            "song_name": song["name"],
            "artist": song["artists"],
            "netease_id": song["id"],
            "share_url": f"https://music.163.com/song?id={song['id']}",
        },
    }
```

### 11.7 部署步骤

**第一阶段: 环境搭建 (一次性) ✅ 已完成**

```bash
# 1. 安装 Node.js API (npm 包)
cd /opt/netease-api
npm init -y && npm install NeteaseCloudMusicApi

# 2. 启动服务
PORT=3000 node node_modules/NeteaseCloudMusicApi/app.js &

# 3. 首次扫码登录
cd /root/character_test
python3 netease_bridge.py --login
# → 终端显示 ASCII 二维码 → 网易云音乐 APP 扫码 → 授权登录
# → cookie 保存到 netease_cookie.txt

# 验证
python3 netease_bridge.py --test   # 显示: API 服务 ✓ 可达, 登录状态 ✓ 已登录
python3 netease_bridge.py --daily  # 获取每日推荐 (自动缓存到 netease_cache.json)
```

**当前状态**: Node.js 服务运行在 `localhost:3000`，已登录用户 `netease_user`，cookie 持久化，缓存正常。`netease_bridge.py` (~230行) 完全解耦于 chiguo_daemon，零新 Python 依赖。

```bash
# 7. 运行决策引擎，观察 music_share 是否出现在 context 中
python3 chiguo_daemon.py --verbose

# 8. 运行完整测试套件
python3 test_chiguo_math.py && python3 test_holiday_parser.py && \
  python3 test_integration.py && python3 test_monitor.py
```

**第四阶段: 接入 OpenClaw cron**

```
# 在 OpenClaw 中更新 chiguo 技能的 cron prompt:
# 当 context 含 music_share 字段时，生成一条包含歌曲分享的自然消息。
# 消息中可提及歌名和歌手，可选附带播放链接。
```

### 11.8 容错设计

| 失败场景 | 降级行为 |
|----------|----------|
| Node.js API 未启动 | `is_available()` → False, topic 不参与选择 |
| Cookie 过期 | `check_api_health()` → False, stderr `[warn]` |
| 缓存过期 + API 不可达 | 返回空, 该日无音乐话题 |
| 每日推荐为空 (新账号) | 返回空 list, 无音乐话题 |
| 网络超时 (5s) | try/except, 返回 None |
| 当日已达 max 推送数 | 返回 None, 其他 7 来源顶上 |
| 夜间时间窗口外 | 返回 None |

**核心原则**: 音乐话题是锦上添花，任何环节失败都不影响 daemon 正常决策。

### 11.9 隐私与合规

- **不存储用户听歌历史**: 只缓存每日推荐列表 (服务端推送的，不是用户历史)
- **不存储用户网易云账号密码**: 仅持久化 cookie token
- **音乐链接不追踪**: 使用 `music.163.com/song?id=X` 公开链接，无追踪参数
- **可撤销**: 在网易云音乐 APP 中移除设备授权即可切断

### 11.10 未来扩展

- 每日推荐歌单 (`/recommend/resource`) → 分享整个歌单
- 主人回复"听过"/"不喜欢" → 调 `/recommend/songs/dislike` 优化推荐
- 歌词话题: 从每日推荐中提取一句歌词作为消息素材
- 纪念日联动: 检测歌单/歌曲与纪念日相关 (如"第一次见面时听的歌")
- 心情匹配: 根据 daemon 情绪 (孤独/开心) 选择不同氛围的歌

---

## 十二、全量代码审计 (2026-06-28)

> 方法：8 路并行 agent 审计全部生产代码、OpenClaw skill 文件、测试文件、配置文件。每路独立阅读完整文件后报告所有 bug/逻辑错误/边界问题。

### 12.1 🔴 CRITICAL (5 项) — 静默失败/数据损坏

| # | 文件:行 | 问题 | 影响 |
|---|---------|------|------|
| C1 | `chiguo_state.py:752,891` | `self.record_observation(...)` 调用在 `ChiguoState` 上，但该方法只存在于 `UserStateEstimator`。`on_user_message` 和 `on_character_message` 两处均因 `AttributeError` 被 `except Exception: pass` 静默吞下 | **Bayesian 在线学习完全死代码**。`observation_history` 永不为空，`BayesianLearner.update_from_label/update_from_implicit` 从未执行，likelihood 表停留在初始默认值 |
| C2 | `chiguo_personality.py:56` | `PersonalityTraits.evolve()` 对 delta 中**所有** dataclass 字段求和（包括默认值 openness=55, conscientiousness=65 等），而非只加显式设置的字段。`PersonalityDelta.WARM_REPLY` 等常量携带完整默认值 | **人格系统完全损坏**。单次交互将所有维度推向 90（clamp 上限），一秒前还是 tsundere 的迟菓变成完美人格 |
| C3 | `chiguo_bayesian.py:457` | `update_from_implicit` 无论 `was_good_timing=True` 还是 `False`，始终用 EMA 向 1.0 方向推 likelihood。`False` 时 factor=0.2 只减慢增速，不降低 | **负反馈信号被反转**。用户沉默被解释为"沉默=喜欢这个状态"，estimator 对所有状态越学越自信 | ✅ 2026-07-31 已修复：bad timing 分支改为真实衰减 `new_val = old * (1 - lr)`（向 0 收敛）；测试断言改为严格下降 |
| C4 | `memory_bridge.py:121-132` | `recent()` 用 epoch-ms 作 SQL `WHERE timestamp >= cutoff_ms`。LanceDB 若存 epoch-s 时间戳，SQL 合法但匹配 0 行，`except Exception` 不触发 `_recent_fallback()` | **记忆查询静默返回空列表**。Ebbinghaus 加权记忆搜索、话题注入的记忆来源全部失效 |
| C5 | `netease_bridge.py:177-183` | 登录成功 (code 803) 后 `resp.get("cookie", "")` 可能返回空字符串（某些 API 版本），但代码仍调用 `_save_cookie("")` 并打印"登录成功" | **假登录成功**。后续所有 API 请求因无有效 cookie 而失败，用户无感知 |

### 12.2 🟠 HIGH (9 项) — 功能损坏/崩溃/数据丢失

| # | 文件:行 | 问题 | 影响 |
|---|---------|------|------|
| H1 | `chiguo_math.py:22,31` | `decay()` 和 `recover()` 未守卫 `half_life_hours <= 0`。传入 0 → `ZeroDivisionError`；负数 → 指数符号翻转，值向错误方向变化 | 配置错误或边界计算可导致崩溃或情绪反转增长 |
| H2 | `chiguo_monitor.py:312-313,367-368` | `avg_reply_latency_h` 和 `median_reply_latency_h` 初始化为 `None` 后从未赋值。`reply_events` 收集了 `silent_hours` 但从未统计 | **监控输出永久为 null**。回复延迟统计功能完全不存在 |
| H3 | `chiguo_monitor.py:508-515` | B3 告警 (`emotion_stuck_low`) 的 `recent_24h` 包含 idle 条目。Idle 条目常无 emotion 数据，`energy` 默认 0 被误计为"低能量" | **B3 假阳性告警**。纯 idle 窗口触发"情绪持续低迷"告警，实际无任何 send 事件 |
| H4 | `anniversary_manager.py:71,167-170` | 2 月 29 日纪念日在非闰年调用 `date.fromisoformat(f"{today.year}-02-29")` 抛 `ValueError`，被 `continue` 静默跳过 | **闰年纪念日每 4 年可见 1 次**。其余 3 年纪念日/倒计时不触发 |
| H5 | `chiguo_sender.py:74-75` | outbox 文件名用 `:{:.0f}` 舍入到整秒。同秒两条消息 → 第二条覆盖第一条 | **消息静默丢失**。高频触发（如连续 send+reply）可能丢消息 |
| H6 | `迟菓语言技巧指南.md:13-15,147` | 概览表将"喵"列为通用语气词（无场景限制），自检项问"是否使用了喵、嘻嘻增强生动性"。SUN2.md 明确禁止通用喵/嘻嘻 | **角色 OOC**。Agent 遵循自检指令会主动使用喵和嘻嘻，偏离原著人格 |
| H7 | `update_holidays.py:95-108` | `try_chinese_calendar` 迭代 `get_holidays(year)` 返回的 namedtuple 用 `.start_date`/`.end_date` 属性，但库实际返回 `.date`/`.name`。`AttributeError` 被 `except Exception` 吞下 | **chinese_calendar 库完全无法使用**。即使库已安装，始终回退到硬编码数据 |
| H8 | `update_holidays.py:120-130` | `offset = year - 2027` 计算后从未使用。农历节假日（春节/端午/中秋）直接复制 2027 日期到任意年份 | **非 2027 年份节假日日期错误**。农历节日每年偏移 ~11 天。2028 春节写到 2 月 6 日，实际约 1 月 26 日 | ✅ 2026-07-02 已修复
| H9 | `test_bayesian.py:199` | `assert new_lik >= old_lik * 0.9` 允许 10% 下降通过。`update_from_implicit` 本身逻辑也有 bug（C3），测试防护形同虚设 | C3 的 bug 在测试中不会被发现 | ✅ 2026-07-31 已修复：断言改为 `new_lik < old_lik`，并新增连续两次 bad timing 持续衰减断言 |

### 12.3 🟡 MEDIUM (25 项)

#### 状态与决策引擎

| # | 文件:行 | 问题 |
|---|---------|------|
| M1 | `chiguo_state.py:310` | `infer_user_state()` 硬编码 `msg_length=10`（始终"medium"），Bayesian 推断的 msg_length 维度无信号 | ✅ 2026-07-02 已修复
| M2 | `chiguo_daemon.py:367` | `max(datetime.fromisoformat(last_msg), datetime.fromisoformat(last_user))` — 若两时间戳时区感知不一致（如一个 naive 一个 aware），Python 抛 `TypeError`，`_tick()` 无异常处理，`evaluate()` 崩溃 |
| M3 | `chiguo_daemon.py:69-74` | 配置热重载重建 `topic_picker`/`composer` 但不失效 `state._bayesian_estimator`。`[bayesian]` 段修改后 `--loop` 模式不生效 |
| M4 | `chiguo_bayesian.py:431` | `update_from_label` 每次将目标值 EMA 推向 1.0，同时对同组其他值做 `(1 - lr*0.5)` 衰减。多次更新后同 `(state, obs_key)` 组内概率和 ≠ 1 | ✅ 2026-07-02 已修复
| M5 | `chiguo_trigger.py:97-136` | 情感触发权重（0.01-0.9 范围）vs. 仪式触发原始权重（morning=2.5, special=3.0）。在仪式时间窗口内，仪式触发概率 >80%，覆盖情感驱动 |
| M6 | `chiguo_trigger.py:169-183` | Longing 触发条件与 `can_send()` longing_override 条件逐字重复。两处独立维护，会漂移 |
| M7 | `chiguo_daemon.py:598-601` | `character_rules` ⑦条铁律缺"喵仅限猫/小白场景""嘻嘻极少用"限制。Agent 以 `character_rules` 为速查时会遗漏 | ✅ 2026-07-02 已修复

#### 配置

| # | 文件:行 | 问题 |
|---|---------|------|
| M8 | `chiguo_proactive.toml:48-51` | `[openclaw]` 段缺 `llm_base_url` 和 `llm_model` key。`chiguo_generator.py` 读这两个 key（有默认值），用户改配置必须在别的段或无法改 |
| M9 | `chiguo_proactive.toml:188-191` | `[bayesian]` 三个 key（`learning_rate`/`min_confidence_for_block`/`utility_threshold`）在 TOML 中定义但 `BayesianLearner` 和 `infer()` 使用硬编码值，配置修改无效 |
| M10 | `chiguo_proactive.toml:19-20` | `ebbinghaus_strength`/`ebbinghaus_min_weight` 有配置值但 `MemoryBridge.__init__()` 不接收参数，`ebbinghaus_weight()` 用硬编码常量 |
| M11 | `chiguo_proactive.toml:208-209` | `[memories].path` key 存在但代码读 `config["memory"]["manual_path"]`（不同的 section 名），`[memories]` 段完全无用 |

#### 调度/节假日

| # | 文件:行 | 问题 |
|---|---------|------|
| M12 | `schedule_parser.py:228` | `week_num = (now.date() - semester_start).days // 7 + 1` — 若 `now` 在 `semester_start` 之前（配置错误或 daemon 提前启动），`week_num <= 0`，所有课程不匹配 |
| M13 | `schedule_parser.py:236` | `if not weeks or week_num in weeks` — 若 `weeks` 为**空集合**（解析失败/缓存损坏），`not weeks` 为 True，课程被视为"每周都有"，与正确行为相反 |
| M14 | `update_holidays.py:138-148` | `get_solar_terms_for` 对 2026/2027 以外年份返回 2027 数据，无偏移。节气每年偏移 ±1 天 | ✅ 2026-07-02 已修复

#### EventBus / Monitor / Watchdog

| # | 文件:行 | 问题 |
|---|---------|------|
| M15 | `chiguo_eventbus.py:14-49` | `_subscribers` 字典无锁。publish 迭代时若 unsubscribe 同时发生 → `RuntimeError: list changed during iteration` |
| M16 | `chiguo_eventbus.py:31-40` | `publish` 静默吞下 handler 所有异常（`except Exception: pass`）。签名错误的 handler 零反馈 | ✅ 2026-07-02 已修复
| M17 | `chiguo_monitor.py:379` | `lancedb_check_count == 0` 条件在外层 `if lancedb_check_count > 0 else None` 包裹内，条件**永远为 False**。意图是"无检查时假定可用"，实际不可达 |
| M18 | `chiguo_monitor.py:67-73` | `_lancedb_path()` 硬编码读 `"chiguo_proactive.toml"` 而非用 `self._monitor_config`。若 `ChiguoMonitor(config_path="/custom/path")` 构造，此方法读错误文件 |
| M19 | `chiguo_watchdog.py:136-153` | `check_lancedb()` 打开 `lancedb.connect()` 连接从不关闭。反复调用可泄漏 fd/线程 | ⚠️ 2026-07-31 前提修正：lancedb 0.34 连接对象**无 close()**，连接由 GC 管理；原 `db.close()` 调用抛 AttributeError 被 catch 成"LanceDB unreachable"恒误报，已改 `hasattr` 防御 |
| M20 | `chiguo_watchdog.py:34-74` | `check_daemon_tick` 返回 dict 结构不一致：ok=True 时无 `"severity"` key，ok=False 时有。下游消费者需防御式 `.get()` |

#### 桥接/发送/生成

| # | 文件:行 | 问题 |
|---|---------|------|
| M21 | `memory_bridge.py:98,126` | `category` 参数直接拼接进 LanceDB SQL：`f"category = '{category}'"`。含单引号的 category → SQL 语法错误或注入 |
| M22 | `chiguo_sender.py:46` | 发送异常被吞后显示"simulated send"，与真正的 demo 模式不可区分。用户看不到错误 |
| M23 | `chiguo_sender.py:88-91` | `_load_log()` 无类型检查。日志文件若损坏为非 list 的 JSON（如 `{}`），`self.history[-200:]` 抛 `TypeError` |
| M24 | `chiguo_generator.py:91` | `llm_base_url` 尾部斜杠 + 路径组合产生双斜杠 URL（`/v1//chat/completions`）。多数服务器容忍但脆弱 |
| M25 | `chiguo_generator.py:110` | LanceDB 触发记忆用 key `lancedb_memory` 但 `_build_situation()` 读 key `memory`。LanceDB 记忆的 `content` 永为空 |

#### OpenClaw Skill 文件

| # | 文件:行 | 问题 |
|---|---------|------|
| M26 | `SKILL.md:77` | 引用 `references/analysis.md`（标定参考），但该文件不存在。Agent 无 calibration 参考 |
| M27 | `SKILL.md:33-41` | context 字段表只列 6 个字段，实际 daemon 输出 15 个字段（缺 `character`/`personality_source`/`schedule_hint` 等 9 个） |
| M28 | `SKILL.md:82-85` | `--user-msg "<哥哥消息原文>"` 若原文含双引号 → shell 解析错误。中文对话中双引号常见 |
| M29 | `SKILL.md:45-54` | 自检清单 9 项缺 SUN2.md §八的波浪线适度和 哼 频率检查 |
| M30 | `SUN2.md:72 vs 101-102` | "嘻嘻"标记为极少使用（10次/17000行），但 Scene 二 示例直接用了"嘻嘻……他说我效率高……"。Agent 从示例可能推断"嘻嘻"可常用 |
| M31 | `memory_bridge.py:15` | 顶层 `import lancedb`。缺失时 daemon 无法启动，与"优雅降级"承诺矛盾 | ✅ 2026-07-31 惰性导入（`_ensure_table()` 内 + `_lancedb` 缓存）
| M32 | `memory_bridge.py:52-58` | `available` 探测失败后进程内永不重试。`--loop` 长驻下 LanceDB 故障恢复后仍禁用 | ✅ 2026-07-31 按 `_RETRY_SECONDS=60` 节流重试
| M33 | `memory_bridge.py:111,153,184` | 结果循环 `row.get("importance", 0) < min_importance`：None → TypeError；NaN → 穿透过滤后 `random.choices` 行为异常 | ✅ 2026-07-31 `_clean_importance()` 统一清洗 + 循环入 try
| M34 | `chiguo_watchdog.py:100` | watchdog state `write_text` 非原子写（半写损坏） | ✅ 2026-07-31 `.tmp` + `os.replace` 原子写
| M35 | `netease_bridge.py:44` | `MUSIC_U` 会话凭据拼进 URL query → 进访问日志 | ✅ 2026-07-31 改 `Cookie` header（v4.32.0 server.js:164-169 支持）
| M36 | `netease_bridge.py:77-83` | `_clean_cookie` 匹配用 `.upper()` 但存储保留原 key；响应给 `__CSRF` 大写时 `wanted.get("MUSIC_U")` 落空 | ✅ 2026-07-31 归一化存储 + 输出统一小写 `__csrf` |
| M37 | `netease_bridge.py:243` | `except (json.JSONDecodeError, Exception)` 子类重复冗余 | ✅ 2026-07-31 简化为 `except Exception` |

### 12.4 🔵 LOW (15 项)

| # | 文件:行 | 问题 |
|---|---------|------|
| L1 | `chiguo_state.py:218-227` | 4 个 dead migration block：`hasattr` 检查的字段在 `CooldownState` dataclass 中已有默认值，永不会缺失 |
| L2 | `chiguo_state.py:825` | `'_anxiety_before_analysis' in dir(self)` 用 `dir()` 而非 `hasattr()`，脆弱且慢 |
| L3 | `chiguo_math.py:86` | 加权随机选择用 `r <= cumulative` 而非 `r < cumulative`。浮点连续分布下实际无差异，但边界语义不精确 |
| L4 | `chiguo_math.py:131` | `hawkes_intensity` 排除 `dt_hours == 0` 的事件（恰好在 now 发生），语义可能令人惊讶 |
| L5 | `chiguo_monitor.py:484-511` | 多处 list comprehension 对同一条目调用两次 `_extract_time(e)`（时间过滤 → 数据提取），浪费解析 |
| L6 | `chiguo_monitor.py:546` | 内层 `len(sends) >= 5` 在外层同名条件块内，冗余（永真） |
| L7 | `chiguo_monitor.py:86,127,136` | `open()`/`read_text()` 无 `encoding="utf-8"`。中文 JSONL 在非 UTF-8 系统上 `UnicodeDecodeError` |
| L8 | `chiguo_daemon.py:310` | 变量 `was_replied` 命名误导：实际含义"上次发送是否被回复"（本次发送前保存），非"本次是否被回复" |
| L9 | `chiguo_proactive.toml:76` | `reply_fast_threshold = 0.08`（4.8 分钟），注释写"≤5 分钟"。5 分钟 = 0.0833h，差 12 秒 |
| L10 | `chiguo_proactive.toml:89` | `rate_energy_min = 5` 为 int，周围值全为 float。类型不一致（实际可用） |
| L11 | `memory_bridge.py:65-67` | `table` property 每次调用 `_ensure_table()`，即使 `_available = False`。无调用者直接使用，但语义不对 |
| L12 | `chiguo_demo.py:171-172` | Reset (`r` 命令) 只重建 `self.state`，`self.gen` 和 `self.sender` 保留旧实例（sender 保留历史） |
| L13 | `chiguo_generator.py:144` | `list(templates.values())[0]` 假设 dict 非空。future 某 trigger 有空 template dict → `IndexError` |
| L14 | `test_monitor.py:77-119` | `test_basic_stats` 断言 `max_unreplied_streak == 0`，所有 entry 用默认 mwr=0，未测试 `messages_without_reply` 追踪路径 |
| L15 | `test_integration.py:126-134` | `avail2 >= 0.05` 过于宽松。若 xskb.xlsx 缺失时 availability 回退到 0.85（正常工作），`>= 0.05` 捕获不到严重 bug |

### 12.5 优先级建议

**立即修复 (CRITICAL, 预计 2-3h)**:
C1 (Bayesian 死代码) → C2 (人格系统损坏) → C3 (负反馈反转) → C4 (记忆查询死代码) → C5 (假登录)

**短期修复 (HIGH, 预计 3-5h)**:
H1 (除零守卫) → H4 (闰年纪念日) → H2/H3 (监控假数据) → H5 (消息覆盖) → H6 (角色 OOC) → H7/H8 (节假日生成)

**下个迭代 (MEDIUM, 分批修复)**:
M8-M11 (死配置) → M1-M3 (推断/热重载/时区) → M15-M16 (线程安全/异常吞没) → M21-M25 (SQL注入/空内容/损坏日志) → M26-M30 (skill 文档)

### 12.6 测试覆盖缺口

当前 132 测试全部通过，但以下关键路径无测试覆盖：
- `PersonalityTraits.evolve()` 多维度不互相污染（已有测试但断言不够严格）
- `UserStateEstimator.record_observation()` 实际被调用（因 C1 bug 从未执行）
- `chiguo_sender` 同秒消息覆盖场景
- 闰年 2 月 29 日纪念日
- Monitor reply latency 统计计算
- `memory_bridge.recent()` 时间戳单位自动检测

### 12.7 2026-07-02 修复记录

本次修复 6 项（1 HIGH + 5 MEDIUM），已确认 5/5 CRITICAL 和 8/9 HIGH 在此前已修复。

- H8: get_holidays_for 农历节假日应用 ~11天/年偏移
- M1: infer_user_state 通过 CooldownState.last_user_msg_length 使用实际消息长度
- M4: update_from_label 归一化移除 max() 钳制，确保概率和 = 1.0
- M7: character_rules 新增规则⑧(喵仅限猫)⑨(嘻嘻极少用)
- M14: get_solar_terms_for 应用 ~0.25天/年节气漂移
- M16: EventBus.publish() 失败 handler 返回 None 而非静默丢弃

### 12.8 2026-07-31 修复记录 — 死代码与接口契约

修复 3 项（1 CRITICAL 语义 + 1 接口契约 + 2 死 import），并确认 1 项死代码存在但不改。

- **C3 语义修复**: `chiguo_bayesian.py` `update_from_implicit` bad-timing 分支原为 no-op（`adjustment=0.0` → `new_val == old`），注释声称"decay toward 0"与实际不符，测试因 `<=` 平凡通过。现改为 `new_val = old * (1 - lr)` 真实向 0 衰减；good-timing 分支不变（EMA 推向 1.0）。`test_bayesian.py` 断言改为严格 `new_lik < old_lik` 并新增连续两次 bad timing 持续衰减验证。该函数仅测试调用（prod 中因 C1 未接线），保留不改（online learning 为设计一部分）
- **Cue 契约修复**: `chiguo_personality.py` `tsundere_style()` 原返回 `tsundere_tsuntsun`/`cool_dere`，不在 `MessageComposer.CUES` 的 8 键集合中，接线即 KeyError。现统一映射到合法键：`tsundere_tsuntsun`→`tsundere_soft`、`cool_dere`→`tsundere_cool`（见映射表）。composer CUES 键保持不动（对外契约）。`test_personality.py` 新增两个重映射分支测试 + 全返回值 ∈ CUES 键集契约测试
- **死 import 清理**: `chiguo_bayesian.py` 删除未使用的 `import math, random`；`chiguo_personality.py` 删除未使用的 `field`
- **确认不改**: `chiguo_state.py:1152` `poisson_should_trigger` 全项目无调用方，属死代码，按范围约束仅记录不删除（注：2026-07-31 后续清理时已删除，见 §12.9）

#### tsundere_style 语义映射表

| 旧返回值 | 语义 | 新返回值 (CUES 合法键) | CUES 描述 |
|---|---|---|---|
| `tsundere_classic` | 经典傲娇——嘴硬攻击型 | `tsundere_classic`（不变） | 嘴硬心软，表面攻击实则关心 |
| `tsundere_tsuntsun` | 嘴硬但温柔底子 | `tsundere_soft` | 软傲娇，语气半软半硬 |
| `tsundere_soft` | 软傲娇——快藏不住了 | `tsundere_soft`（不变） | 偶尔泄漏真感情后立刻嘴硬掩饰 |
| `tsundere_cool` | 酷娇——冷淡但偶尔温柔 | `tsundere_cool`（不变） | 冷淡外表偶尔暴露温柔 |
| `dere_dere` | 娇——防线基本融化 | `dere_dere`（不变） | 直接表达感情，撒娇 |
| `cool_dere` | 酷娇——独立但关心 | `tsundere_cool` | 冷淡外表偶尔暴露温柔 |

验证：`test_bayesian.py` 18/18、`test_personality.py` 19/19、`test_composer.py` 10/10 全过；临时脚本确认 8 组人格取值返回值全 ∈ CUES 键集，bad timing 连续两次严格下降（0.400 → 0.360 → 0.324），good timing 仍增强。

M3（热重载重置Bayesian）和 M5（仪式权重支配情绪权重）保留为设计决策，有配置逃生舱口。

## 2026-06-30 — v5 Robustness (crash recovery + time-gap)

**触发**: 运行日志分析 — daemon 已死 21.6h，6/28 崩溃循环导致状态重置、限频绕过、用户回复丢失

**P0 (Critical)**:
- `.bak` 状态备份: save前shutil.copy2→恢复时先试.bak再删除
- `fsync`: write_text后os.fsync确保落盘
- 长时间停机dampen: >24h → 24 + (elapsed-24)*0.5
- `accumulated_lambda` 类型修复: None→0.0，清除所有or-guard

**P1 (High)**:
- `tick_seq` 计数器: STATE_VERSION=5，每次save递增
- tmp验证: os.replace前验证JSON有效
- OSError保护: save()失败不崩溃
- 概率累积移到save之前: 防止崩溃丢失held_count

**P2 (Medium)**:
- 损坏审计: chiguo_state_audit.jsonl
- snapshot加messages_without_reply
- watchdog读取tick_seq
- monotonic时钟防护: 壁钟跳变检测

**已修复审计bug**: _tick() naive datetime→tz补全, 双import清理, r+b→rb

## 2026-06-30 (续2) — 四个已知局限修复

- `chiguo_watchdog.py` — tick_seq 前向比对：持久化到 `chiguo_watchdog_state.json`，>3h 停滞→warn
- `chiguo_daemon.py` — PID 锁文件：`chiguo_loop.pid`，进程存活检测，`finally` 清理
- `chiguo_state.py` — 校验和：`save()` SHA256→`_checksum`，`_load()` 验证不匹配记录审计但不拒绝加载（**v5 时点行为；v6 起改为不匹配 → raise ValueError 强制 .bak 恢复链，见 2026-07-31"遗留 MAJOR 修复"记录**）
- `chiguo_daemon.py` — 时钟异常日志：`clock_backward` / `clock_jump_forward` 写入 stderr + audit
