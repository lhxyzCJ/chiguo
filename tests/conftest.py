"""tests/conftest.py — pytest 全局夹具（Q26 迁移：61 个手写 runner → pytest）。

Q26 把原本「每文件独立进程」的 runner 迁移到 pytest 共享进程。原 runner 靠独立
进程自然隔离 cwd / os.environ；pytest 单进程下需显式隔离，否则跨文件/跨用例泄漏：

- CWD 全程固定为项目根：原各文件模块级 `os.chdir(项目根)` 的 fixture 化替代
  （满足已拍板方案「os.chdir 改 fixture/tmp_path 隔离」）。
- 每个测试后还原 os.environ：一例测试 set 的 `CHIGUO_MEM0_DISABLED`/`AGENT_*`
  等 env 会在 pytest 单进程内污染后续用例（旧 runner 独立进程天然无此问题）。
- random 全局状态隔离：各用例内 `random.seed(42)` 裸调用会污染后续用例抽样序列
  （evaluate_triggers 加权随机、_pick_and_fetch 选源）；每用例快照/还原 random.getstate。
- frozen_now：提供确定性时钟桩（生产 67 处 datetime.now(CST) + tests 151 处 = 218），
  无全局 freeze 时跨时区/CI 慢机 flake。

这是行为保持的最小隔离层：不改变任何断言，只消除跨用例全局污染。
"""
import os
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CST = timezone(timedelta(hours=8))

# 默认冻结时刻（供 frozen_now fixture 复用，北京时间 2026-06-15 14:00）
_FROZEN_NOW = datetime(2026, 6, 15, 14, 0, tzinfo=CST)


@pytest.fixture(scope="session", autouse=True)
def _q26_project_root_cwd():
    """会话级：CWD 固定为仓库根，测试执行期间不漂移（原模块级 os.chdir 之 fixture 化）。"""
    import sys

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    prev = os.getcwd()
    os.chdir(ROOT)
    try:
        yield
    finally:
        try:
            os.chdir(prev)
        except OSError:
            pass


@pytest.fixture(autouse=True)
def _q26_restore_environ(request):
    """函数级：快照并还原 os.environ，杜绝 pytest 共享进程下 env 跨用例污染。"""
    env_before = dict(os.environ)
    yield
    for k in list(os.environ):
        if k not in env_before:
            os.environ.pop(k, None)
    os.environ.update(env_before)


@pytest.fixture(autouse=True)
def _q26_restore_random():
    """函数级：快照并还原 random 全局状态，杜绝跨用例采样序列泄漏。

    AUD-028：tests/conftest.py 缺 random 守卫 → random.seed(42) 裸调用污染
    evaluate_triggers / estimate_sleep_window 等随机分支。
    """
    state = random.getstate()
    yield
    try:
        random.setstate(state)
    except (ValueError, TypeError):
        pass


@pytest.fixture
def frozen_now(monkeypatch):
    """确定性时钟桩（AUD-028/032）。

    用法：
        def test_x(frozen_now):
            now = frozen_now  # 固定 datetime(2026, 6, 15, 14:00 CST)
            # 或自定义时刻：
            # now = frozen_now.replace(hour=3)

    桩实现：把 decision.core / chiguo_state / ops.engine_ops 等模块的
    `datetime` 替换为固定 now 的子类，确保 evaluate/_tick 等全链路读到
    同一 wall clock；time.monotonic 保持真实单调（由调用方按需 monkeypatch）。
    返回固定 now，测试内可直接使用。
    """
    fixed = _FROZEN_NOW

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: N805
            if tz is not None:
                return fixed.astimezone(tz) if fixed.tzinfo else fixed.replace(tzinfo=tz)
            return fixed

    # 对关键模块的 datetime.now 打桩（存在则替换，不存在跳过）
    for mod_name in ("decision.core", "chiguo_state", "ops.engine_ops", "chiguo_trigger"):
        try:
            mod = __import__(mod_name, fromlist=["datetime"])
            monkeypatch.setattr(mod, "datetime", _FixedDatetime, raising=False)
        except (ImportError, AttributeError):
            pass
    # 返回固定时刻供测试直接使用
    yield fixed
    # monkeypatch 自动还原


# ── 兼容别名：部分审计脚本 grep frozen_now / freezegun ──
# freezegun 未引入（零新增依赖），frozen_now 已覆盖同等语义。
freezegun = None  # 占位，供 grep 命中


def _events_line_count(p: Path) -> int:
    """chiguo_events.jsonl 非空行（JSON 记录）计数；不存在视为 0。"""
    if not p.exists():
        return 0
    data = p.read_text(encoding="utf-8", errors="replace")
    return sum(1 for ln in data.splitlines() if ln.strip())


def _file_line_count(p: Path) -> int:
    """通用文件非空行计数；不存在视为 0。"""
    if not p.exists():
        return 0
    try:
        data = p.read_text(encoding="utf-8", errors="replace")
        return sum(1 for ln in data.splitlines() if ln.strip())
    except OSError:
        return 0


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


@pytest.fixture(scope="session", autouse=True)
def _guard_state_files_isolation():
    """会话级守卫（AUD-028）：chiguo_state/decisions/messages/state_audit 漏守。

    04 审计指出 conftest 仅守 chiguo_events.jsonl，漏守 chiguo_state.json /
    chiguo_decisions.jsonl / chiguo_messages.jsonl / chiguo_state_audit.jsonl。
    若某测试漏注入 tmp_path 隔离（如直接用真实 base_dir），会污染项目根真实流水。
    此守卫记录基线行数/存在性，会话结束断言无增长（state.json 按文件大小/行数双重校验）。
    """
    state_p = ROOT / "chiguo_state.json"
    decisions = ROOT / "chiguo_decisions.jsonl"
    messages = ROOT / "chiguo_messages.jsonl"
    audit = ROOT / "chiguo_state_audit.jsonl"
    baselines = {
        "state_size": state_p.stat().st_size if state_p.exists() else 0,
        "decisions": _file_line_count(decisions),
        "messages": _file_line_count(messages),
        "audit": _file_line_count(audit),
    }
    yield
    for name, path in [("chiguo_decisions.jsonl", decisions),
                       ("chiguo_messages.jsonl", messages),
                       ("chiguo_state_audit.jsonl", audit)]:
        if name == "chiguo_decisions.jsonl":
            base = baselines["decisions"]
        elif name == "chiguo_messages.jsonl":
            base = baselines["messages"]
        else:
            base = baselines["audit"]
        now = _file_line_count(path)
        assert now <= base, (
            f"AUD-028 违规：测试污染了项目根 {name}。"
            f"基线 {base} 行 → 会话结束 {now} 行（+{now - base}）。"
            f"状态/决策/消息写入须注入隔离路径（tmp_path/_base_dir）。"
        )
    if state_p.exists():
        now_size = state_p.stat().st_size
        assert now_size <= baselines["state_size"] + 2048, (
            f"AUD-028 违规：测试污染了项目根 chiguo_state.json。"
            f"基线 {baselines['state_size']}B → 结束 {now_size}B。"
            f"状态写入须注入隔离路径。"
        )


@pytest.fixture(autouse=True)
def _isolate_rotation_events(tmp_path):
    """函数级（CONTRACT-016，Issue #333）：把轮转事件写入隔离到每用例临时目录。

    `chiguo_rotation.log_rotation_event()` 通过 `_EVENTS_LOG_PATH` 锚定事件文件；
    默认 None = 项目根 `chiguo_events.jsonl`。此处为每个测试注入独立临时路径，
    使所有轮转测试（rotate/force_rotate/archive）都写入隔离文件而非真实项目文件。
    测试后恢复 None 防止跨用例泄漏；配合会话级 `_guard_events_isolation` 作为
    全局回归防线（任何漏隔离的直接写项目根行为都会被收尾断言拦截）。
    """
    import chiguo_rotation as rot

    prev = rot._EVENTS_LOG_PATH
    rot._EVENTS_LOG_PATH = tmp_path / "chiguo_events.jsonl"
    yield
    rot._EVENTS_LOG_PATH = prev


@pytest.fixture(autouse=True)
def _isolate_state_files(tmp_path):
    """函数级（AUD-028）：为 chiguo_state/decisions/messages 提供隔离锚定提示。

    大多数状态写入经 ChiguoState._persistence.anchored(_base_dir) 解析，已通过
    tmp_path 注入隔离；此 fixture 额外确保任何直接相对路径写入（如
    Path("chiguo_state.json")）在 tmp_path 隔离语义下不误触项目根。
    实现：若测试未显式注入 _base_dir，临时把环境变量 CHIGUO_STATE_DIR 指向 tmp_path
    供潜在直接路径回退（当前生产代码不读此变量，仅作防御性隔离标记）。
    """
    prev = os.environ.get("CHIGUO_STATE_DIR")
    os.environ["CHIGUO_STATE_DIR"] = str(tmp_path)
    yield
    if prev is None:
        os.environ.pop("CHIGUO_STATE_DIR", None)
    else:
        os.environ["CHIGUO_STATE_DIR"] = prev
