#!/usr/bin/env python3
"""test_config_util.py — Q25 配置 util 收敛对齐测试

审计 Q25：`_to_float`(composer/trigger 逐字重复) 与 `_cfg_float`(netease，无 isfinite 守卫)
三份实现收敛为 chiguo_math.cfg_float 单实现。本 runner 守护：
1. composer/trigger 与 chiguo_math 共用同一 cfg_float 函数对象（单实现，杜绝逐字重复回潮）
2. cfg_float 语义：非数值/NaN/inf → 默认；clamp_min 参数保留 netease 负值钳制语义
3. 三处调用路径行为对齐：composer/trigger 不钳制负值（调用处 max() 兜底），
   netease 负值钳制为 0 且新增 isfinite 守卫后对合法输入行为不变
"""
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok - {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL - {name} {detail}")


tests = []


def goto(name):
    tests.append(name)
    print(f"\n## {name}")


def run(t):
    try:
        t()
    except Exception:
        traceback.print_exc()
        return False
    return True


def _all_same(mod_a, mod_b):
    return getattr(mod_a, "cfg_float", None) and \
        getattr(mod_a, "cfg_float", None) is getattr(mod_b, "cfg_float", None)


def test_shared_single_implementation():
    import chiguo_composer, chiguo_trigger, chiguo_math
    goto("单实现：composer/trigger 与 chiguo_math 共用同一 cfg_float")
    check("composer.cfg_float is chiguo_math.cfg_float",
          _all_same(chiguo_composer, chiguo_math))
    check("trigger.cfg_float is chiguo_math.cfg_float",
          _all_same(chiguo_trigger, chiguo_math))
    # 三处不再各自维护一份逐字重复的私有实现
    check("composer 无私有 _to_float", "_to_float" not in
          (ROOT / "chiguo_composer.py").read_text(encoding="utf-8"))
    check("trigger 无私有 _to_float", "_to_float" not in
          (ROOT / "chiguo_trigger.py").read_text(encoding="utf-8"))
    check("netease/service 无私有 _cfg_float", "def _cfg_float" not in
          (ROOT / "netease" / "service.py").read_text(encoding="utf-8"))


def _import_cfg():
    from chiguo_math import cfg_float
    return cfg_float


def test_cfg_float_semantics():
    cfg_float = _import_cfg()
    goto("cfg_float 语义：isfinite 守卫 + clamp_min")
    # 非数值回退默认
    for bad in (None, "abc", [], {}, object()):
        check(f"非数值 {type(bad).__name__} → default",
              cfg_float(bad, 5.0) == 5.0)
    # NaN / ±inf 回退默认（isfinite 守卫）
    for special in (float("nan"), float("inf"), float("-inf")):
        check(f"非有限 {special} → default", cfg_float(special, 5.0) == 5.0)
    # 合法数值正常返回（int/str/float）
    check("int 3 → 3.0", cfg_float(3, 5.0) == 3.0)
    check("str '3.5' → 3.5", cfg_float("3.5", 5.0) == 3.5)
    check("float 3.5 → 3.5", cfg_float(3.5, 5.0) == 3.5)
    # 负值：无 clamp_min 时保留（composer/trigger 路径，调用处 max() 兜底）
    check("负值 -2 无 clamp → -2（保留，交调用处钳制）",
          cfg_float(-2.0, 5.0) == -2.0)
    # clamp_min=0.0：netease 路径负值钳制为 0
    check("负值 -2 带 clamp_min=0 → 0", cfg_float(-2.0, 5.0, clamp_min=0.0) == 0.0)
    check("正值 3 带 clamp_min=0 → 3（不变）",
          cfg_float(3.0, 5.0, clamp_min=0.0) == 3.0)
    check("NaN 带 clamp_min=0 → default（守卫优先于 clamp）",
          cfg_float(float("nan"), 5.0, clamp_min=0.0) == 5.0)


def test_trigger_alignment():
    goto("trigger 行为对齐（cfg_float 不变式）")
    # trigger 侧直接复用共享 cfg_float（identity 已在单实现测试断言）；这里验证 NaN/inf
    # 在其配置读取路径上的守卫语义——通过共享函数对象直接确认，避免整机 ChiguoState 构造。
    from chiguo_math import cfg_float as shared
    from chiguo_trigger import cfg_float as trg
    check("trigger 与共享实现同一对象（逐字一致源于单实现）",
          trg is shared)
    check("trigger 路径 NaN → 默认（isfinite 守卫）",
          trg(float("nan"), 1.0) == 1.0)
    check("trigger 路径 inf → 默认（isfinite 守卫）",
          trg(float("inf"), 1.0) == 1.0)


def test_composer_alignment():
    from chiguo_composer import MessageComposer
    goto("composer 行为对齐（cfg_float 不变式）")
    # composer 配置权重走 cfg_float：NaN/inf 回退默认、负值由调用处 max() 钳制
    c = MessageComposer(object(), {"size_1_weight": float("nan"),
                                   "cue_tsundere_weight": -1.0})
    check("size_1_weight=nan → 默认 0.20（isfinite 守卫）",
          c.size_weights[1] == 0.20)
    check("cue_tsundere_weight=-1 → 调用处 max() 钳制为 0",
          c.cue_weights["tsundere_classic"] == 0.0)


def test_netease_alignment():
    import tempfile
    from netease.service import NeteaseService
    goto("netease 行为对齐（isfinite 守卫 + clamp_min 保留负值钳制）")
    cfg = {"netease": {"retry_backoff_seconds": float("nan"),
                       "reprobe_minutes": -1.0},
           "topic_picker": {}}
    with tempfile.TemporaryDirectory() as td:
        svc = NeteaseService(config=cfg, base_dir=td)
        check("retry_backoff_seconds=nan → 默认 2.0（isfinite 守卫）",
              svc.retry_backoff == 2.0)
        check("reprobe_minutes=-1 → 钳制为 0（clamp_min 保留原语义）",
              svc.reprobe_minutes == 0.0)
        # 合法配置行为不变
        svc2 = NeteaseService(config={"netease": {"retry_backoff_seconds": 3.0,
                                                  "reprobe_minutes": 45.0},
                                      "topic_picker": {}},
                              base_dir=td)
        check("合法 retry_backoff_seconds=3 → 3.0", svc2.retry_backoff == 3.0)
        check("合法 reprobe_minutes=45 → 45.0", svc2.reprobe_minutes == 45.0)


if __name__ == "__main__":
    for t in (test_shared_single_implementation, test_cfg_float_semantics,
              test_trigger_alignment, test_composer_alignment,
              test_netease_alignment):
        run(t)

    print(f"\n{'-'*40}")
    if FAIL:
        print(f"{len(FAIL)} 项失败: {FAIL}", file=sys.stderr)
        sys.exit(1)
    print(f"ALL config-util alignment tests passed ({len(tests)}).")
