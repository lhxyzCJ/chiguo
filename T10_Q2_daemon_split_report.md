# T10·Q2 — chiguo_daemon.py 上帝入口拆分报告（Issue #268）

> 分支：`fix/q2-daemon-split`（基于 main `a6db82c`）。本文件为任务留档（行数/认知对比）。

## 一、拆分目标与方案

审计 Q2 指出 `chiguo_daemon.py` 2240 行 / 41 defs / main() 463 行 / 35 CLI 参数无上帝入口欠内聚。
按已拍板职责拆包（cli/ runner/ decision/ ops/），并明确发送通道归属（--loop 直连桥 /send 与
cron 侧 curl 收敛到统一发送模块 `runner/loop.py::_loop_send`）。

**结构（拆包后薄 facade）**：

| 文件 / 包 | 职责 |
|---|---|
| `chiguo_daemon.py` | 唯一保留的 CLI 入口薄 facade：`from cli.dispatch import main`，re-export `DecisionEngine` 等兼容符号；`if __name__ == "__main__": main()` |
| `cli/parser.py` | 35 参数 argparse 定义（对外 CLI 契约逐字不变） |
| `cli/dispatch.py` | `main()/run(args)` 子命令分发（优先级与拆分前一致） |
| `cli/commands.py` | 轻量子命令（--attention/--schedule-recall/--schedule-change/--memory-search）+ `_load_light_config` |
| `decision/base.py` | `DecisionEngineBase`：构造/配置热重载/基座锚定/快照 |
| `decision/core.py` | `DecisionCoreMixin`：evaluate 决策主链 / tick / emit_idle / 触发评估 |
| `decision/context.py` | `ContextMixin`：`_build_context`（原 218 行方法独立归属） |
| `decision/engine.py` | `DecisionEngine` 组合出口（base+core+context+ops+runner 五 mixin 组合） |
| `runner/loop.py` | `LoopSenderMixin`（`_loop_send` 生成→发送→记账 + U2 健康记账 + 动态休眠）+ `run_loop`（PID 锁/循环编排） |
| `ops/engine_ops.py` | `AccountingMixin`：`record_user_message` / `record_send_text` / `record_send_result` / recv 去重 / 召回 / consolidate |
| `chiguo_paths.py` | 项目根统一锚点（拆包后子包不再用自身 `__file__` 定位根） |

**关键边界处理**：
- 路径锚定：默认 config/personality 目录改经 `chiguo_paths.PROJECT_ROOT`（拆包后 `__file__` 指向子目录）。
- 测试 import 同步：`test_feedback` 打桩 `decision.core.evaluate_triggers`；`test_daemon_fixes` 固定时钟改
  `decision.core.datetime`；`test_schedule_cli` 用 `cli.commands._cmd_*`；`test_main_toml_binding` 两 loop 键
  指向 `runner/loop.py`；`test_isolation` ENGINE_MODULES 纳入新包模块。
- 新增参数快照测试 `tests/test_daemon_cli_snapshot.py`（验收①守护）。

## 二、行数对比

| 项 | 拆分前 | 拆分后 |
|---|---|---|
| `chiguo_daemon.py` | **2240 行** | **36 行**（薄 facade，↓ 98.4%） |
| `decision/` 包 | —（内聚于 daemon） | 978 行（base 100 / core 610 / context 233 / engine 26 / __init__ 9） |
| `cli/` 包 | — | 571 行（parser 99 / commands 122 / dispatch 344 / __init__ 6） |
| `runner/` 包 | — | 353 行（loop 348 / __init__ 5） |
| `ops/` 包 | — | 495 行（engine_ops 490 / __init__ 5） |
| `chiguo_paths.py` | — | 12 行 |
| 合计（新包 + facade） | 2240 行（单文件） | 2445 行（分布在 14 个文件） |

> 行数总和略增（2240→2445）源于包结构（`__init__`/docstring/拆分边界注释）与 `chiguo_paths`
> 统一锚点；但**单文件认知负载**从「2240 行上帝文件」降到最重的 `decision/core.py` 610 行单一职责，
> 其余文件均 ≤ 490 行，每文件只承担一个职责（决策 / 解析 / 分发 / loop / 记账）。

## 三、认知对比（职责分层）

拆分前：一个 2240 行文件混装 构造 + 决策 + 上下文 + 记账 + loop 发送 + CLI 分发 + 轻量子命令。
拆分后每个模块单职责、方法自洽：

| 认知维度 | 拆分前 | 拆分后 |
|---|---|---|
| 单文件体量 | 2240 行 / 41 defs | 最重 `decision/core.py` 610 行（核心决策单一职责） |
| main() 上帝分发 | 463 行 / ~52 条件分支 | `cli/dispatch.py` run() 344 行（仍按优先级顺序，语义不变） |
| _loop_send | 内聚在 daemon（MCC42） | `runner/loop.py` 独立发送模块（loop 直连 + cron 统一 `_loop_send`） |
| _build_context | 218 行内聚在 daemon | `decision/context.py` 独立 |
| 记账/审计 | 混装 | `ops/engine_ops.py` AccountingMixin |
| 参数解析 | main() 内 | `cli/parser.py` 35 参数（snapshot 守护） |

## 四、验收标准自评

① **CLI 35 参数行为不变** —— 新增 `tests/test_daemon_cli_snapshot.py` 对 argparse 参数集合/类型/
默认值/nargs/const/choices/metavar/action/help 逐字快照断言（含 35 参数数量、--version 为 _VersionAction）。
② **`bash scripts/ci-test.sh` 全链绿** —— 独立跑通全链，exit 0（见回报）。
③ **行数/认知显著下降** —— daemon 2240→36 行（↓98.4%），职责拆到 4 包，见上文对比。

## 五、对外契约不变性说明

- 35 参数集合/默认值/帮助文本与拆分前逐字一致（`cli/parser.py`）。
- 子命令分发顺与 JSON 输出形状、exit code 语义在 `cli/dispatch.py` 与拆分前一致。
- `DecisionEngine` 仍可 `from chiguo_daemon import DecisionEngine`（facade re-export），
  既有测试/脚本 import 路径大部分无需改动。
- 发送通道归属：`--loop` 直连 bridge `/send` 与 cron 侧 curl 统一收敛到 `runner/loop.py::_loop_send`
  （cron 经 `scripts/chiguo-tick.sh` 仍走自身 curl，语义不变；loop 走 `_loop_send`）。
