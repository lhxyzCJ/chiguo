"""tests/conftest.py — pytest 全局夹具（Q26 迁移：61 个手写 runner → pytest）。

Q26 把原本「每文件独立进程」的 runner 迁移到 pytest 共享进程。原 runner 靠独立
进程自然隔离 cwd / os.environ；pytest 单进程下需显式隔离，否则跨文件/跨用例泄漏：

- CWD 全程固定为项目根：原各文件模块级 `os.chdir(项目根)` 的 fixture 化替代
  （满足已拍板方案「os.chdir 改 fixture/tmp_path 隔离」）。
- 每个测试后还原 os.environ：一例测试 set 的 `CHIGUO_MEM0_DISABLED`/`AGENT_*`
  等 env 会在 pytest 单进程内污染后续用例（旧 runner 独立进程天然无此问题）。

这是行为保持的最小隔离层：不改变任何断言，只消除跨用例全局污染。
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session", autouse=True)
def _q26_project_root_cwd():
    """会话级：CWD 固定为仓库根，测试执行期间不漂移（原模块级 os.chdir 之 fixture 化）。"""
    prev = os.getcwd()
    os.chdir(ROOT)
    yield
    os.chdir(prev)


@pytest.fixture(autouse=True)
def _q26_restore_environ(request):
    """函数级：快照并还原 os.environ，杜绝 pytest 共享进程下 env 跨用例污染。"""
    env_before = dict(os.environ)
    yield
    for k in list(os.environ):
        if k not in env_before:
            os.environ.pop(k, None)
    os.environ.update(env_before)


def _events_line_count(p: Path) -> int:
    """chiguo_events.jsonl 非空行（JSON 记录）计数；不存在视为 0。"""
    if not p.exists():
        return 0
    data = p.read_text(encoding="utf-8", errors="replace")
    return sum(1 for ln in data.splitlines() if ln.strip())


@pytest.fixture(scope="session", autouse=True)
def _guard_events_isolation():
    """会话级守卫（CONTRACT-016 测试隔离，Issue #333）。

    `chiguo_rotation.log_rotation_event()` 默认把轮转事件锚定到`chiguo_rotation.py`
    所在目录（= 项目根）的 `chiguo_events.jsonl`。未注入隔离路径的轮转测试会真实
    追加写入该文件——污染本地运行流水文件、干扰时序指标。

    守卫在会话开始记录项目根 `chiguo_events.jsonl` 的基线行数，结束断言"无增长"：
    任何轮转测试漏注入隔离路径都会使会话收尾断言失败，作为全局回归防线。
    """
    events = ROOT / "chiguo_events.jsonl"
    baseline = _events_line_count(events)
    yield
    now = _events_line_count(events)
    assert now <= baseline, (
        f"CONTRACT-016 违规：进度轮转测试污染了项目根 chiguo_events.jsonl。"
        f"基线 {baseline} 行 → 会话结束 {now} 行（+{now - baseline}）。轮转事件写入须注入隔离路径。"
    )
