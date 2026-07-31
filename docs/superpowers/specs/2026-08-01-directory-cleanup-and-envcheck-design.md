# 目录整理 + 环境检查脚本设计(v10.1)

日期:2026-08-01
状态:已批准(用户确认设计 A + 设计 B)

## 1. 背景与目标

v10 已完成多机可移植性修复(路径 ~ 化、deploy.sh)。遗留两个问题:

1. **根目录混乱**:git 跟踪的数据/资源文件(`xskb.xlsx` 课表、`chiguo_memories.json` 手动记忆、`netease_qr.png` 登录二维码)与 21 个代码模块混在根目录
2. **缺环境就绪检查**:新机器 pull 后,无法一键确认「OpenClaw 是否装好、依赖是否齐、lancedb-pro 是否存在」——deploy.sh 只有散装提示,watchdog 只管运行时健康

本次目标(用户确认):
1. 数据/资源文件移入 `data/` 子目录,代码模块保持根目录不动
2. 新增独立 `chiguo_envcheck.py` 环境检查 CLI(5 组检查项),deploy.sh 调用它去重

## 2. 已确认决策

| # | 决策 |
|---|---|
| 1 | 仅移动数据/资源文件到 `data/`(课表/手动记忆/二维码);代码模块不动(21 个 .py 保持根目录) |
| 2 | 检查脚本为独立 `chiguo_envcheck.py`,不扩展 watchdog、不并入 deploy.sh |
| 3 | 检查项 5 组:Python/uv 版本、OpenClaw+skill 目录、LanceDB lancedb-pro、网易云 API+登录、数据文件完整 |
| 4 | 不检查:git 同步状态、daemon 运行新鲜度、定时任务注册(归 watchdog/deploy.sh 职责) |
| 5 | 输出 JSON + 汇总退出码,遵循项目 CLI 惯例(JSON→stdout,诊断→stderr) |
| 6 | 退出码与 watchdog 一致:0=就绪,1=有 warn,2=有 critical |
| 7 | 检查全只读:不建目录、不写缓存、不启动服务;网易云检查发轻量健康请求(尊重 `NETEASE_API_BASE`) |

## 3. 设计 A — 目录整理

### 3.1 文件移动

```
data/xskb.xlsx            # 课表(原根目录,git mv)
data/chiguo_memories.json # 手动记忆(原根目录,git mv)
data/netease_qr.png       # 网易云登录二维码输出(原根目录,git mv)
```

### 3.2 同步改动

所有路径解析沿用既有 `_base_dir` 相对锚定逻辑,不引入新机制:

| 文件:行 | 改动 |
|---|---|
| `chiguo_proactive.toml:19` | `manual_path = "data/chiguo_memories.json"` |
| `chiguo_proactive.toml:171` | `xlsx_path = "data/xskb.xlsx"` |
| `chiguo_state.py:235` | 默认 `xlsx_path` → `"data/xskb.xlsx"` |
| `chiguo_state.py:322` | 默认 `manual_path` → `"data/chiguo_memories.json"` |
| `schedule_parser.py:36` | 默认 `xlsx_path` → `"data/xskb.xlsx"` |
| `schedule_parser.py:438` | CLI 默认 → `"data/xskb.xlsx"` |
| `netease_bridge.py:172` | QR 输出锚定 `模块目录/data/netease_qr.png` |

注:`memories_path`/课表路径均经 `_anchored()`(相对路径锚 base_dir,绝对路径保留),toml 改相对值即可;测试无直接引用这些文件名(已核查),全量回归兜底。

### 3.3 不做的事

- 运行时文件(state/日志)不挪——gitignore 已隔离,且 `_anchored` 面广,动它们收益低
- 代码包化不做——21 个模块 import 关系全耦合根目录,大重构不值
- `archive/`、`personality/`、`doc/` 已子目录化,不动

## 4. 设计 B — chiguo_envcheck.py

### 4.1 CLI

```
uv run python chiguo_envcheck.py           # 全量检查:JSON → stdout,汇总退出码 0/1/2
```

遵循项目惯例:JSON 到 stdout,诊断到 stderr。无子命令,无参数(默认即 JSON 输出)。

### 4.2 检查项(5 组,按序)

| # | name | 检查内容 | critical/warn 判定 |
|---|---|---|---|
| 1 | `env` | `sys.version_info >= (3,14)`;`shutil.which("uv")` | 版本不足/uv 缺失 → critical(3.14-only 语法必挂) |
| 2 | `openclaw` | `~/.openclaw` 目录存在;`~/.openclaw/workspace/skills/chiguo/` 下 `SUN2.md`+`SKILL.md` 存在 | 目录缺失 → critical(消息生成端不在);skill 文件缺 → warn(降级:无人格设定,OpenClaw 仍可发消息) |
| 3 | `lancedb` | lancedb 可导入;`~/.openclaw/memory/lancedb-pro` 存在且 `open_table("memories")` 只读成功 | lancedb 未装 → warn(JSON 降级可用);库存在但不可连 → warn;路径不存在 → warn |
| 4 | `netease` | API 可达(轻量健康请求,URL 取 `NETEASE_API_BASE` 默认 `http://localhost:3000`);`netease_cookie.txt` 存在;`netease_health.json` 状态只读 | API 不可达 → warn(话题源降级,不影响主流程);cookie 缺 → warn(首次需 `--login`);全部降级仍可运行 → 无 critical |
| 5 | `data` | `data/xskb.xlsx`、`data/chiguo_memories.json` 存在且可读 | 缺失 → warn(课表→availability=0.85 降级;记忆→空) |

### 4.3 输出 schema

```json
{
  "checks": [
    {"name": "env", "ok": true, "severity": "ok", "detail": "Python 3.14.6, uv 0.12.0"},
    {"name": "lancedb", "ok": false, "severity": "warn", "detail": "lancedb not installed → JSON fallback"}
  ],
  "summary": {"ok": 4, "warn": 1, "critical": 0},
  "ready": false
}
```

- `severity`: `ok`/`warn`/`critical`;`ready = (critical == 0)`
- 退出码:`0` 全部 ok;`1` 有 warn;`2` 有 critical(与 watchdog 约定一致)
- 每项独立 try/except,单项失败不中断后续检查

### 4.4 实现要点

- 零外部依赖(纯标准库),与 daemon 同目录;`os.path.expanduser` 解析 `~`
- LanceDB 检查复用 `memory_bridge.MemoryBridge` 的惰性导入模式(import lancedb 失败→warn 不崩);只读探测:存在则 `open_table("memories")` + 读 `table.schema`,失败→warn
- 网易云健康检查:urllib 轻量 GET `{API_BASE}/login/status`,超时 5s,异常→warn;cookie/health 文件只读检查
- 路径读取点与 daemon 一致:toml 的 `xlsx_path`/`manual_path`/`lancedb_path` 值从 `chiguo_proactive.toml` 读取(而非硬编码),保持单一事实来源;`[monitor]` 未定义 `lancedb_path` 时回退 `[memory]`(与 v10 monitor 修复同语义)

### 4.5 测试

`test_envcheck.py`(第 19 个测试文件,独立 runner):
- 每个检查项独立测试:构造临时目录/假 `~`(`expanduser` 可测性:检查函数接受可注入的 home 参数或 env var)
- 全 ok → 退出码 0;warn → 1;critical → 2(子进程调用验证)
- 异常输入(权限拒绝、损坏 jsonl)不崩

注:测试隔离沿用既有约定(`tempfile.TemporaryDirectory` + 固定种子),不触碰真实 `~/.openclaw`。

## 5. deploy.sh 集成

- 第 4 节(OpenClaw 检查)+ 第 5 节(网易云检查)替换为 `uv run python chiguo_envcheck.py` 调用,按退出码输出摘要
- 保留:uv/Python 安装、18 测试、迁移提示、cron 指引

## 6. 职责分工(避免重复)

| 工具 | 职责 |
|---|---|
| `chiguo_envcheck.py` | 环境就绪:装没装、配没配、依赖齐不齐(新机器/日常排查) |
| `chiguo_watchdog.py` | 运行时健康:daemon 在跑吗、日志新鲜度、磁盘、LanceDB 连通 |
| `deploy.sh` | 一键部署:装环境 → envcheck → 18 测试 → 提示 |

## 7. 验证

- 全量 19 个测试文件通过(18 既有 + test_envcheck)
- `git mv` 后 `uv run python chiguo_daemon.py` 单次决策正常(课表/记忆路径解析到 data/)
- 从 /tmp 运行 envcheck 正确锚定项目文件
- 模拟缺失场景(watchdog 依赖项):OpenClaw 目录缺失 → critical;lancedb 未装 → warn
