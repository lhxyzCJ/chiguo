# 迟菓系统部署指南

> 面向从零部署；本文是操作手册不是教程。系统文档见 [SYSTEM.md](SYSTEM.md)；pi 集成细节见 [PI_INTEGRATION.md](PI_INTEGRATION.md)。
> 仓库公开，任何人都可为自己部署；系统只与一个用户聊天（下文称「哥哥」，人格设定的一部分）。

## 一、这是什么

一个零 LLM 的数学决策引擎（`chiguo_daemon.py`）决定何时、以什么心情主动发消息，pi-agent（LLM）按人格生成微信消息，wechat-bridge 负责收发；全部计算在本机完成。默认由 crontab 每 15 分钟评估一次，需要时才调模型。

> 微信触达走官方 iLink Bot 通道（上游 [Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin) 开源协议），扫码登录正规 API，无封号风险；隐私数据（登录态/对话/状态）仅存本机，不进 git。

## 二、外部依赖（3 个 GitHub 公开仓库）

| 仓库 | 用途 | 落点 | 由谁安装 |
|------|------|------|----------|
| github.com/lhxyzCJ/wechatbot（wechatbot iLink SDK 的个人 fork） | 微信登录/收发 SDK | `$HOME/wechatbot` | `wechat-bridge.sh install` 自动 clone + npm 安装 |
| github.com/lhxyzCJ/TestForPi-memory-lancedb-pro（pi 记忆扩展 fork） | 对话记忆自动沉淀/召回（autoCapture/autoRecall） | `~/.pi-agent/TestForPi-memory-lancedb-pro` | `scripts/install_pi.sh` 阶段 1 |
| github.com/NeteaseCloudMusicApiEnhanced/api-enhanced（锁 v4.39.0 tag） | 网易云数据源（可选） | `/opt/netease-api` | `scripts/netease-api.sh install` |

前两个是必需，第三个可选跳过（`--skip-netease`）。

## 三、前提条件

- Debian Linux + systemd（root 或可 sudo——微信桥自启与网易云服务要写 systemd）
- git / curl（deploy.sh 自装 uv 需要 curl）/ Node.js + npm（wechatbot SDK 与记忆扩展都要构建）
- 模型 API key（`export PI_API_KEY=...` 再跑部署）
- 可选：ollama（`qwen3-embedding:0.6b` 嵌入模型，记忆扩展用；缺则记忆降级，不影响主链路）
- 默认端口：18790（微信桥 /send）/ 3000（网易云 API）/ 11434（ollama）

## 四、分级部署路径

| 档位 | 内容 | 命令 |
|------|------|------|
| T0 纯本地（无模型无微信） | 决策引擎 CLI 全功能 | `bash deploy.sh --skip-pi --skip-bridge --skip-netease` |
| T1 加模型后端 | +消息生成（pi-agent） | `bash deploy.sh --skip-bridge --skip-netease` |
| T2 完整系统 | 全部（微信/记忆/网易云/crontab） | `bash deploy.sh` |

低档位可事后补装：`bash scripts/install_pi.sh --yes` / `bash scripts/wechat-bridge.sh install`；T0 也完全可玩：见 README 快速开始。

## 五、完整部署六步（T2）

### 1. uv + Python 3.14

自动装到 `~/.local/bin`；创建 `.venv`。验证：`uv run python --version`。

### 2. 全量自检

`bash scripts/ci-test.sh`：36 个 Python + 10 个脚本测试，任一失败即中止。验证：看输出「全部测试通过 ✓」。

该脚本与 GitHub Actions 全链 CI（`.github/workflows/ci.yml`，每次 push/pull_request 自动跑）共用同一入口：本地任何一次 `git push` 都会在 CI 上重跑同一链条。CI 环境注意点：runner 非 root（`tests/test_service.sh` 用 fake `id` 注入 root 视角）、无 `/usr/bin/node`（node 测试用 `process.execPath`）、无 `@wechatbot/wechatbot` 与 `data/xskb.xlsx`（`ci-test.sh` 自动自举 stub 与最小课表 fixture），因此本地与 CI 结果一致。

### 3. 环境检查

`uv run python chiguo_envcheck.py`：exit 0=就绪 / 1=警告可运行 / 2=严重先修复。可 `--skip-pi` 跳过 pi 检查。

### 4. 微信桥

`bash scripts/wechat-bridge.sh install` + `bash scripts/service.sh autostart` → systemd `chiguo-bridge.service`（同时注册 ollama 自启）。随后扫码登录：`bash scripts/wechat-bridge.sh login`。可 `--skip-bridge`。

### 5. pi 环境

`bash scripts/install_pi.sh`（七阶段：探测 → 记忆扩展 clone+build → settings.json → json5 配置 → ollama 检查 → auth.json 写 key → crontab 注册 + 冒烟）。先 `export PI_API_KEY=...`；可 `--skip-pi`；`bash scripts/install_pi.sh --dry-run` 只扫描不修改。

### 6. 网易云 API 服务（可选）

`bash scripts/netease-api.sh install` → systemd `netease-api.service`（需 root）；扫码登录 `uv run python -m netease.bridge --login`。可 `--skip-netease`。

部署完成后 crontab 有两条：`*/15 chiguo-tick.sh` → `logs/cron-tick.log`、`*/15 replan-tick.sh` → `logs/cron-replan.log`。

## 六、机器落点地图

| 路径 | 内容 | 进 git | 可迁移（新机器拷贝即用） |
|------|------|--------|--------------------------|
| 仓库根（clone 位置） | 系统本体 | 是 | git clone |
| `$HOME/wechatbot` | wechatbot SDK（iLink） | 否 | 重装自动 clone |
| 仓库内 `wechat-bridge/node_modules` + `.env` | bridge 依赖与运行环境 | 否 | install 重建 |
| `~/.chiguo/auth/`（`wechat/credentials.json`、`netease_cookie.txt`、`pi-auth.json`） | 微信登录态/网易云 cookie/pi key 迁移源 | 否 | 拷贝即用（微信/网易云跨设备可能自动重登一次） |
| `~/.pi-agent/`（`TestForPi-memory-lancedb-pro/` + `~/.pi-agent/memory/lancedb-pro` 记忆库） | 记忆扩展 + LanceDB | 否 | 拷贝 |
| `~/.pi/agent/`（`auth.json` / `models.json` / `memory-lancedb-pro.json5`） | pi 配置与 key | 否 | 拷贝 |
| `/opt/netease-api` | 网易云 API 服务 | 否 | 重装 |
| systemd：`chiguo-bridge.service` / `netease-api.service` | 常驻服务 | - | 部署时注册 |
| crontab 2 条 | tick + replan-tick | - | install_pi.sh 注册 |

## 七、首次配置

1. 微信扫码：`bash scripts/wechat-bridge.sh login`（二维码打印在桥日志）
2. 网易云扫码（可选）：`uv run python -m netease.bridge --login`
3. pi key：install_pi.sh 阶段 5 已写 `~/.pi/agent/auth.json`；换 key 重跑 `bash scripts/install_pi.sh --yes`
4. 检查 toml `[host]` 的 provider/model（`chiguo_proactive.toml`）；`[wechat].wechat_recipient` 为占位符，登录后自动注入真实 openid，无需手改

## 八、验证清单

```bash
uv run python chiguo_envcheck.py          # exit 0
bash scripts/wechat-bridge.sh status      # 运行中 + 登录态存在
bash scripts/chiguo-tick.sh               # 端到端冒烟：15 分钟内微信应收到消息；idle 静默退出也算正常
uv run python -m netease.bridge --test    # 可选
uv run python chiguo_daemon.py --stats --alerts --monitor
```

## 九、迁移与备份

- 备份/迁移清单：`~/.chiguo/auth/`、`~/.pi-agent/`、`~/.pi/`、仓库内运行时文件（`chiguo_state.json`、`chiguo_decisions.jsonl`、`chiguo_messages.jsonl`、`schedule_overrides.json`、`schedule_plan.json`、`schedule_clarify.json`、`anniversaries.json`、`break_state.json`）、`data/`（课表/手动记忆）
- 新机器：clone → 拷贝上述 → `bash deploy.sh`（自动接入；pi key 100% 迁移可用；微信/网易云跨设备自动重登兜底）

## 十、常见问题

- 微信桥装好了但收不到消息？（看桥日志 `logs/wechat-bridge.log`，路径见 `bash scripts/wechat-bridge.sh start` 输出；扫码态失效 → `bash scripts/wechat-bridge.sh login` 重登）
- tick 跑过没消息？（看 `logs/cron-tick.log`；决策门控正常——深夜/静默窗口不发）
- pi 报 No API key / 401？（install_pi.sh 阶段 5 重写 key；`uv run python chiguo_envcheck.py` 复核）
- 网易云服务起不来？（`bash scripts/netease-api.sh status`；可选组件可跳过）

更多故障现象 → 处理见 [PI_INTEGRATION.md 九、故障排查](PI_INTEGRATION.md#九故障排查)（本处不重复）。
