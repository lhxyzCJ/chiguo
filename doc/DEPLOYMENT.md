# 迟菓系统部署指南（v1.15）

> 面向从零部署；本文是操作手册不是教程。系统文档见 [SYSTEM.md](SYSTEM.md)；agent 集成细节见 [AGENT_INTEGRATION.md](AGENT_INTEGRATION.md)。
> 仓库公开，任何人都可为自己部署；系统只与一个用户聊天（下文称「哥哥」，人格设定的一部分）。

## 一、这是什么

一个零 LLM 的数学决策引擎（`chiguo_daemon.py`）决定何时、以什么心情主动发消息，agent 后端（LLM）按人格生成微信消息，wechat-bridge 负责收发；全部计算在本机完成。默认由 crontab 每 15 分钟评估一次，需要时才调模型；也可切换为 `CHIGUO_DAEMON_LOOP=1` 常驻形态（决策引擎 `--loop` 常驻 + agent RPC 常驻，见 §六、部署形态）。

> 微信触达走官方 iLink Bot 通道（上游 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 开源协议），扫码登录正规 API，无封号风险；隐私数据（登录态/对话/状态）仅存本机，不进 git。

## 二、外部依赖（2 个 GitHub 公开仓库）

| 仓库 | 用途 | 落点 | 由谁安装 |
|------|------|------|----------|
| github.com/lhxyzCJ/wechatbot（wechatbot iLink SDK 的个人 fork） | 微信登录/收发 SDK | `$HOME/wechatbot` | `wechat-bridge.sh install` 自动 clone + npm 安装 |
| github.com/NeteaseCloudMusicApiEnhanced/api-enhanced（跟随上游最新 tag） | 网易云数据源（可选） | `/opt/netease-api` | `netease-api.sh install` |

wechatbot 必需，网易云可选跳过（`--skip-netease`）。

## 三、前提条件

- Debian Linux + systemd（root 或可 sudo——微信桥自启与网易云服务要写 systemd）
- git / curl（deploy.sh 自装 uv 需要 curl）/ Node.js + npm（wechatbot SDK 要构建）
- Python 3.14+（deploy.sh 用 uv 安装；当前版本 v1.15）
- 模型 API key（`export AGENT_API_KEY=...` 再跑部署）
- 可选：ollama（`qwen3-embedding:0.6b` 嵌入模型，mem0 记忆用；缺则记忆降级，不影响主链路）
- 默认端口：18790（微信桥 /send）/ 3000（网易云 API）/ 11434（ollama）

## 四、脚本职责一览

| 脚本 | 职责 |
|------|------|
| `deploy.sh`（仓库根） | 一键部署：uv+Python 3.14+依赖 → mem0 校验 → 全量自检 → 环境检查 → 微信桥/agent/网易云分级安装 → 迁移提示 |
| `scripts/ci-test.sh` | 全量自检链（59 py + 13 script），与 GitHub Actions 共用同一入口 |
| `scripts/chiguo-tick.sh` | 主动发送链（crontab 触发：决策 → 生成 → 微信发送 → 健康记录） |
| `scripts/replan-tick.sh` | 计划重分析（crontab 触发） |
| `scripts/install_agent.sh` | agent 环境安装（key 写入、crontab/常驻注册、冒烟） |
| `scripts/agent-auth.sh` | 从 `~/.pi/agent/auth.json` 解析 API key 到 `OPENCODE_API_KEY`（供 tick 脚本 source） |
| `scripts/service.sh` | 微信桥 systemd 自启/临时/状态/停止/卸载 |
| `scripts/wechat-bridge.sh` | 微信桥安装/启动/停止/状态/扫码登录 |
| `scripts/netease-api.sh` | 网易云 API 服务安装/启动/停止/状态 |

## 五、分级部署路径

| 档位 | 内容 | 命令 |
|------|------|------|
| T0 纯本地（无模型无微信） | 决策引擎 CLI 全功能 | `bash deploy.sh --skip-agent --skip-bridge --skip-netease` |
| T1 加模型后端 | +消息生成（agent 后端） | `bash deploy.sh --skip-bridge --skip-netease` |
| T2 完整系统 | 全部（微信/记忆/网易云/crontab） | `bash deploy.sh` |

低档位可事后补装：`bash scripts/install_agent.sh --yes` / `bash scripts/wechat-bridge.sh install`；T0 也完全可玩：见 README 快速开始。

## 六、完整部署步骤（T2，对应 deploy.sh 六步 + 拆分小节）

### 1. uv + Python 3.14 + 依赖

自动装到 `~/.local/bin`；首次部署建 `.venv` 并 `uv sync --all-extras`（安装 mem0 记忆与 openpyxl 课表解析）。验证：`uv run python --version`。

### 2. mem0 依赖校验

deploy.sh 检查 mem0 是否可导入（mem0 为当前唯一记忆后端，缺失即中止）。

### 3. 全量自检

`bash scripts/ci-test.sh`：59 个 Python + 13 个脚本测试（其中 8 个 Node `.mjs` + 5 个 Shell，独立 runner、无 pytest），任一失败即中止。验证：看输出「ALL TESTS PASSED」。

该脚本与 GitHub Actions 全链 CI（`.github/workflows/ci.yml`，每次 push/pull_request 自动跑）共用同一入口：本地任何一次 `git push` 都会在 CI 上重跑同一链条。CI 环境注意点：runner 非 root（`tests/test_service.sh` 用 fake `id` 注入 root 视角）、无 `/usr/bin/node`（node 测试用 `process.execPath`）、无 `@wechatbot/wechatbot` 与 `data/xskb.xlsx`（`ci-test.sh` 自动自举 stub 与最小课表 fixture），因此本地与 CI 结果一致。

### 4. 环境检查

`uv run python chiguo_envcheck.py`：exit 0=就绪 / 1=警告可运行 / 2=严重先修复。可 `--skip-agent` 跳过 agent 检查（agent 缺失降级为警告，不阻断）。

### 5. 微信桥

`bash scripts/wechat-bridge.sh install` + `bash scripts/service.sh autostart` → systemd `chiguo-bridge.service`（同时注册 ollama 自启）。随后扫码登录：`bash scripts/wechat-bridge.sh login`。可 `--skip-bridge`。

### 6. agent 环境与定时（crontab 或常驻二选一）

`bash scripts/install_agent.sh`（阶段：探测 → ollama 检查 → auth 写 key → crontab/常驻注册 + 冒烟）。先 `export AGENT_API_KEY=...`；可 `--skip-agent`；`bash scripts/install_agent.sh --dry-run` 只扫描不修改。

**切换 loop 常驻**：`CHIGUO_DAEMON_LOOP=1 bash scripts/install_agent.sh --yes` → 移除 chiguo-tick crontab（防与常驻双发）+ 安装 systemd `chiguo-daemon.service`（`--loop 900 --compact`）。

### 7. 网易云 API 服务（可选）

`bash scripts/netease-api.sh install` → systemd `netease-api.service`（需 root）；扫码登录 `uv run python -m netease.bridge --login`。可 `--skip-netease`。

部署完成后 crontab 有两条：`*/15 chiguo-tick.sh` → `logs/cron-tick.log`、`*/15 replan-tick.sh` → `logs/cron-replan.log`。**loop 常驻形态下** crontab 仅剩 replan-tick（tick 条目互斥移除）。

## 七、机器落点地图

| 路径 | 内容 | 进 git | 可迁移（新机器拷贝即用） |
|------|------|--------|--------------------------|
| 仓库根（clone 位置） | 系统本体 | 是 | git clone |
| `$HOME/wechatbot` | wechatbot SDK（iLink） | 否 | 重装自动 clone |
| 仓库内 `wechat-bridge/node_modules` + `.env` | bridge 依赖与运行环境 | 否 | install 重建 |
| `~/.chiguo/auth/`（`wechat/credentials.json`、`netease_cookie.txt`、`agent-auth.json`） | 微信登录态/网易云 cookie/agent key 迁移源 | 否 | 拷贝即用（微信/网易云跨设备可能自动重登一次） |
| `data/mem0/`（qdrant 嵌入式向量库 + history.db，gitignore） | mem0 记忆层 | 否 | 拷贝 |
| `~/.pi/agent/`（`auth.json` / `models.json`） | agent 配置与 key（含 mem0 LLM key 来源） | 否 | 拷贝 |
| `/opt/netease-api` | 网易云 API 服务 | 否 | 重装 |
| systemd：`chiguo-bridge.service` / `netease-api.service` / `chiguo-daemon.service`（loop 形态） | 常驻服务 | - | 部署时注册 |
| crontab 2 条（loop 形态 1 条） | tick + replan-tick（loop 仅 replan） | - | install_agent.sh 注册 |

## 八、部署形态（默认 cron / 可选 loop 常驻）

两种形态**互斥**（install_agent.sh 阶段 6 处理，防双发消息）：

| | 默认（cron） | loop 常驻（`CHIGUO_DAEMON_LOOP=1`） |
|---|---|---|
| 决策评估 | crontab `*/15` tick.sh 冷启动 | systemd `chiguo-daemon.service`（`--loop 900` 动态休眠，PID 锁 + 配置热重载） |
| agent 调用 | tick 冷启动 spawn agent-run；回复链 spawn（`WECHAT_BRIDGE_AGENT_RPC=1` 时回复链 RPC 优先） | 发送侧 `_loop_send` 经 bridge `/agent/prompt` 转发常驻 agent RPC；回复链同样 RPC 优先 |
| 失败回退 | spawn（既有链） | RPC 失败自动回退 spawn（不变式） |
| 回退 cron | - | `systemctl disable --now chiguo-daemon.service` + `CHIGUO_DAEMON_LOOP=0` 重跑 install_agent.sh |

## 九、首次配置

1. 微信扫码：`bash scripts/wechat-bridge.sh login`（二维码打印在桥日志）
2. 网易云扫码（可选）：`uv run python -m netease.bridge --login`
3. agent key：install_agent.sh 阶段 6 已写 `~/.pi/agent/auth.json`；换 key 重跑 `bash scripts/install_agent.sh --yes`
4. 检查 toml `[host]` 的 provider/model（`chiguo_proactive.toml`）；`[wechat].wechat_recipient` 为占位符，登录后自动注入真实 openid，无需手改

## 十、验证清单

```bash
uv run python chiguo_envcheck.py          # exit 0
bash scripts/wechat-bridge.sh status      # 运行中 + 登录态存在
bash scripts/chiguo-tick.sh               # 端到端冒烟：15 分钟内微信应收到消息；idle 静默退出也算正常
uv run python -m netease.bridge --test    # 可选
uv run python chiguo_daemon.py --stats --alerts --monitor
```

## 十一、迁移与备份

- 备份/迁移清单：`~/.chiguo/auth/`（含 `agent-auth.json`）、`~/.pi/`、仓库内运行时文件（`chiguo_state.json`、`chiguo_decisions.jsonl`、`chiguo_messages.jsonl`、`schedule_overrides.json`、`schedule_plan.json`、`anniversaries.json`、`break_state.json`、`holidays.json`、`schedule_cache.json`）、`data/`（课表/记忆/手动数据）
- 新机器：clone → 拷贝上述 → `bash deploy.sh`（自动接入；agent key 100% 迁移可用；微信/网易云跨设备自动重登兜底）
- 微信登录态跨设备实测可复用：迁移后轮询正常，但首次**主动发送**可能被服务端拒（`[send error] prepare failed`，context_token 过期）——从微信给机器人发一条消息刷新 token 即恢复，无需重新扫码。
- 课表数据：仓库无 `data/` 目录，`chiguo_proactive.toml` 的 `xlsx_path = "data/xskb.xlsx"` 由部署者自行放入（最小 fixture 由 ci-test.sh 自动自举）。

## 十二、从旧版本（<v1.15）升级

> 覆盖旧版本原地升级到 v1.15。从零部署见「五、分级部署路径」。项目不保留向后兼容（过时路径直接删），升级按「旧代码不兼容、删旧上新」执行，涉及**覆盖式**操作，务必按序备份。

1. **备份本地定制与运行时文件**：仓库内**跟踪**文件中的本地定制 `chiguo_proactive.toml` 会被 `git fetch && git reset --hard origin/main` 覆盖，先备份；运行时文件（`chiguo_state.json`、`chiguo_decisions.jsonl`、`chiguo_messages.jsonl`、`schedule_*.json`、`anniversaries.json`、`break_state.json`、`holidays.json`、`schedule_cache.json` 等，清单见「十一、迁移与备份」）**git 不跟踪**（见 .gitignore）——`git reset --hard` 不会动它们，但也没有版本保护，误删/损坏只能靠备份/拷贝恢复；`~/.chiguo/auth/`（登录态）与 `~/.pi/agent/`（agent key）在仓库外不受影响。
2. **`.env` 键名迁移**：v1.7（pi 时代）的 `WECHAT_BRIDGE_PI_RUN` 等已废弃，v1.15 改用 `WECHAT_BRIDGE_AGENT_RUN`（agent 运行脚本路径）+ `WECHAT_BRIDGE_AGENT_RPC`（回复链 RPC 常驻开关，`=1` 启用）；完整 `PIRUN_*`/`PI_*` → `AGENTRUN_*`/`AGENT_*` 映射见 [AGENT_INTEGRATION.md「0.2 env 映射」](AGENT_INTEGRATION.md#02-env-映射)。升级后必须重跑 `bash scripts/wechat-bridge.sh install`（幂等，按新键名重写 `wechat-bridge/.env`，保留已配置 token 与登录态，不必重新扫码）。
3. **显式重启服务**：`bash scripts/service.sh autostart` 只注册开机自启（`systemctl enable --now` 不会重启已运行实例）；升级后显式 `systemctl restart chiguo-bridge`，否则旧代码进程不换新。
4. **crontab 会被恢复**：`bash scripts/install_agent.sh`（阶段 6/6b）会把 chiguo-tick 与 replan-tick 条目按当前路径整行替换/注册——若之前手动注释禁用或改过路径，升级后会被恢复为启用状态，注意核对（loop 形态反向切换还会停用 `chiguo-daemon.service`）。
5. **网易云两套登录态**：chiguo 侧 cookie（`~/.chiguo/auth/netease_cookie.txt`）与 `/opt/netease-api`（api-enhanced）服务内部登录态各自独立、分别校验；升级后分别确认，失效则重扫 `uv run python -m netease.bridge --login`（经本地 API 服务完成，两处状态一起刷新）。
6. **记忆不迁移**：v1.15 前的 pi/lancedb 记忆**不迁移**，v1.15 起 mem0 从零开始（唯一记忆后端）；如需保留旧记忆先备份旧记忆库目录。

## 十三、常见问题

- 微信桥装好了但收不到消息？（看桥日志：systemd 形态 `journalctl -u chiguo-bridge`；直接启动形态 `/tmp/opencode/wechat-bridge.log`；扫码态失效 → `bash scripts/wechat-bridge.sh login` 重登）
- tick 跑过没消息？（看 `logs/cron-tick.log`；决策门控正常——深夜/静默窗口不发）
- agent 报 No API key / 401？（install_agent.sh 阶段 6 重写 key；`uv run python chiguo_envcheck.py` 复核）
- 网易云服务起不来？（`bash scripts/netease-api.sh status`；可选组件可跳过）

更多故障现象 → 处理见 [AGENT_INTEGRATION.md 十一、故障排查](AGENT_INTEGRATION.md#十一故障排查)（本处不重复）。
