#!/usr/bin/env python3
"""test_trigger_config_defaults.py — Q10 (#276) 默认值等价测试

验收标准：#276 把硬编码魔法权重/概率提到 [trigger] toml 后，**缺省配置下行为与现值
（硬编码值）完全一致**。本 runner 用「剥离新键」法证明等价：
  1. 构造一个不含任何 Q10 新键的 [trigger] 配置（模拟升级前的「缺省」配置）→ 代码走默认参数
  2. 与含新键默认值（= 现值）的真实 toml 配置对比
  3. 同一组随机种子逐一跑 evaluate_triggers → 断言输出 trigger 类型序列逐一相同

只要代码 fallback 默认参数与 toml 现值一致，两者必须逐种子等价；任一不一致会暴露。
"""
import os
import re
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState
from chiguo_trigger import evaluate_triggers

# 与 test_trigger.py::_make_state 同款：真实 toml + 临时目录锚定 + mem0 禁用
def _make_state(tmp: str, src_toml_text: str, now: datetime, **overrides) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(src_toml_text)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    s = ChiguoState(cfg)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    for k, v in overrides.items():
        if hasattr(s.emotion, k):
            setattr(s.emotion, k, v)
        elif hasattr(s.cooldown, k):
            setattr(s.cooldown, k, v)
    return s


# Q10 在 [trigger] 中新增的配置键（TOML 隔离部分）。
# #393 收敛后仅剩 5 键在 toml；另 11 键已删（走代码 fallback，见 P393_REMOVED_KEYS）。
Q10_KEYS = [
    "followup_memory_probability", "habit_probability",
    "playful_base_weight", "reflect_base_weight", "reflect_probability",
]

# 每个键的代码 fallback 默认值（必须 == toml 现值 = 原硬编码值）
EXPECTED_DEFAULTS = {
    "followup_memory_probability": 0.5, "habit_probability": 0.06,
    "playful_base_weight": 0.15, "reflect_base_weight": 0.08,
    "reflect_probability": 0.08,
}

# #393 收敛删去的 11 键：ritual 权重 6 + 时间窗口概率 3 + mem0 浮现 2。
# fallback 默认值 = 原 toml 现值 = 原硬编码值，删键行为恒等。
P393_REMOVED_KEYS = {
    "ritual_special_weight": 3.0, "ritual_morning_weight": 2.5,
    "ritual_night_weight": 2.0, "ritual_meal_weight": 0.8,
    "ritual_memory_weight": 2.0, "ritual_mem0_weight": 1.5,
    "morning_probability": 0.10, "night_probability": 0.12,
    "meal_probability": 0.05,
    "mem0_surface_min_silent_hours": 6.0, "mem0_surface_probability": 0.08,
}


def _strip_q10_keys(toml_text: str) -> str:
    """把 [trigger] 中 Q10 新增键的整行删掉 → 模拟缺省（升级前）配置，其余不动。"""
    pat = re.compile(r"(?m)^(?:%s)\s*=.*(?:\n|$)" % "|".join(Q10_KEYS))
    return pat.sub("", toml_text)


def test_q10_toml_values_equal_fallbacks():
    """三重守护之外的另一层保险：true 配置现值必须等于代码 fallback 默认值，
    否则「缺省配置行为=现值」不成立。比较真实 toml [trigger] 现值 vs 期望现值。"""
    cfg = tomllib.loads(Path("chiguo_proactive.toml").read_text(encoding="utf-8"))
    trg = cfg.get("trigger", {})
    for key, expected in EXPECTED_DEFAULTS.items():
        assert key in trg, f"[trigger].{key} 缺失"
        actual = float(trg[key])
        assert actual == expected, \
            f"[trigger].{key} toml 现值({actual}) != 代码默认({expected})"
    print("  OK test_q10_toml_values_equal_fallbacks")


def test_q10_missing_keys_identical_to_present():
    """剥离新键的缺省配置 vs 含新键默认配置：相同种子序列 evaluate_triggers 输出逐一相同。"""
    src = Path("chiguo_proactive.toml").read_text(encoding="utf-8")
    stripped = _strip_q10_keys(src)
    assert stripped != src, "剥离应产生变化（Q10 键确在 toml 中）"
    with tempfile.TemporaryDirectory() as td_present, tempfile.TemporaryDirectory() as td_stripped:
        now = datetime(2026, 6, 15, 9, 0, tzinfo=CST)  # 早安窗口 → 覆盖 ritual 概率路径
        s_present = _make_state(td_present, src, now, energy=40, loneliness=75)
        s_stripped = _make_state(td_stripped, stripped, now, energy=40, loneliness=75)
        # 散列多种场景各跑一段种子序列，提升覆盖（morning 概率/仪式权重/情绪候选）
        scenarios = [
            dict(now=datetime(2026, 6, 15, 9, 0, tzinfo=CST), n=120, seed0=100),
            dict(now=datetime(2026, 6, 15, 14, 0, tzinfo=CST), n=120, seed0=200),
            dict(now=datetime(2026, 6, 15, 20, 0, tzinfo=CST), n=120, seed0=300),
        ]
        for sc in scenarios:
            for i in range(sc["n"]):
                random.seed(sc["seed0"] + i)
                ta = evaluate_triggers(s_present, sc["now"])
                random.seed(sc["seed0"] + i)  # 复位种子 → 两配置消费相同随机序列
                tb = evaluate_triggers(s_stripped, sc["now"])
                ka = ta.type if ta else "None"
                kb = tb.type if tb else "None"
                assert ka == kb, \
                    f"场景 now={sc['now']} seed={sc['seed0']+i}: 缺省配置行为漂移 {ka} != {kb}"
    print("  OK test_q10_missing_keys_identical_to_present")


def test_p393_removed_keys_absent_and_fallback_identical():
    """#393：11 键已从 toml 删除；fallback 默认 = 原现值（删键行为恒等）。

    两重守护：① toml [trigger] 无此 11 键；② 代码 fallback 默认值与原现值一致
    （读 chiguo_trigger.py 的 .get("<key>", <default>) 默认参数）。"""
    import re
    cfg = tomllib.loads(Path("chiguo_proactive.toml").read_text(encoding="utf-8"))
    trg = cfg.get("trigger", {})
    for key in P393_REMOVED_KEYS:
        assert key not in trg, f"[trigger].{key} 应已删除"
    trg_src = Path("chiguo_trigger.py").read_text(encoding="utf-8")
    for key, expected in P393_REMOVED_KEYS.items():
        m = re.search(r'\.get\("%s",\s*([^)]+)\)' % re.escape(key), trg_src)
        assert m, f"chiguo_trigger.py 缺 {key} 的 fallback 读取"
        actual = float(m.group(1))
        assert actual == expected, \
            f"{key} fallback({actual}) != 原现值({expected})，删键不等价"
    print("  OK test_p393_removed_keys_absent_and_fallback_identical")


if __name__ == "__main__":
    print("test_trigger_config_defaults.py\n")
    tests = [test_q10_toml_values_equal_fallbacks, test_q10_missing_keys_identical_to_present,
             test_p393_removed_keys_absent_and_fallback_identical]
    _prev = os.environ.get("CHIGUO_MEM0_DISABLED")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    failed = 0
    try:
        for t in tests:
            try:
                t()
            except Exception as e:
                print(f"  FAIL {t.__name__}: {e}")
                import traceback
                traceback.print_exc()
                failed += 1
    finally:
        if _prev is None:
            os.environ.pop("CHIGUO_MEM0_DISABLED", None)
        else:
            os.environ["CHIGUO_MEM0_DISABLED"] = _prev
    print(f"\n{'='*40}\nALL {len(tests)} tests, {failed} failed.")
    sys.exit(1 if failed else 0)
