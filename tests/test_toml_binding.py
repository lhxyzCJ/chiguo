#!/usr/bin/env python3
"""test_toml_binding.py — personality/*.toml 接线测试（Phase 2 Task 7）

测试意图（brief Step 1）：toml 存在、meta.name=迟菓、composer 加载逻辑生效
（cue ↔ 模板关联：tsundere_* → tsundere.toml，dere_dere → deredere.toml，
参考台词注入 compose_situation）。

注：brief 示例中 `MessageComposer()` 无参构造与本仓库签名不符
（__init__ 要求 state），按实际实现适配为 make_composer()。
"""

import sys, os
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

random.seed(42)  # 固定种子 → 概率断言确定性（同 test_topics.py 做法）

import pathlib
import tomllib
from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from chiguo_composer import MessageComposer
from chiguo_personality import PersonalityTraits


class MockState:
    """与 test_composer.py 一致的最小 mock ChiguoState"""
    def __init__(self, personality=None):
        self._personality = personality
        self._exam_ranges = []

    @property
    def personality(self):
        return self._personality

    @property
    def exam_ranges(self):
        return self._exam_ranges

    def schedule_status(self, now):
        return {"in_class": False, "class_load": "free", "remaining_classes": 0}

    def holiday_parser(self):
        class Mock:
            def is_holiday(self, now): return False
        return Mock()
    holiday_parser = property(holiday_parser)


def make_composer():
    state = MockState(PersonalityTraits())
    return MessageComposer(state, {})


def test_toml_files_exist():
    """两个 toml 存在、可解析、含 meta + trigger_templates 段"""
    for name in ("tsundere.toml", "deredere.toml"):
        p = pathlib.Path(__file__).parent.parent / "personality" / name
        assert p.exists(), f"缺少 personality/{name}"
        with open(p, "rb") as f:
            data = tomllib.load(f)
        assert "meta" in data, f"{name} 缺 meta"
        assert "trigger_templates" in data, f"{name} 缺 trigger_templates"
    print("  OK test_toml_files_exist")


def test_meta_name_is_chiguo():
    """toml meta.name=迟菓（按 toml id 查询，加载逻辑生效）"""
    c = make_composer()
    assert c.cue_meta("tsundere")["name"] == "迟菓"
    assert c.cue_meta("deredere")["name"] == "迟菓-融化"
    print("  OK test_meta_name_is_chiguo")


def test_cue_meta_by_cue_id():
    """cue 名也可查 meta：tsundere_* → tsundere.toml，dere_dere → deredere.toml"""
    c = make_composer()
    for cue in ("tsundere_classic", "tsundere_soft", "tsundere_cool", "trade_tsundere"):
        assert c.cue_meta(cue)["name"] == "迟菓", cue
    assert c.cue_meta("dere_dere")["name"] == "迟菓-融化"
    print("  OK test_cue_meta_by_cue_id")


def test_trigger_templates_loaded():
    """composer 加载了 toml 的 trigger_templates（原著例句）"""
    c = make_composer()
    tmpl = c.cue_templates["tsundere_classic"]["trigger_templates"]
    assert any("鸡肉三明治" in line for line in tmpl["good_morning"]), "good_morning 应为原著报单风例句"
    assert any("没等你" in line for line in tmpl["loneliness"]), "loneliness 应为原著被冷落例句"
    dere = c.cue_templates["dere_dere"]["trigger_templates"]
    assert any("凭什么" in line for line in dere["loneliness"]), "dere 应为原著崩溃段例句"
    print("  OK test_trigger_templates_loaded")


def test_template_lines_for():
    """cue + 触发类型 → 参考台词；未覆盖类别/非 tsundere_*/dere → 空"""
    c = make_composer()
    lines = c._template_lines_for("tsundere_classic", "lonely_low")
    assert lines and any("没等你" in l or "不告诉你" in l for l in lines)
    crash = c._template_lines_for("dere_dere", "lonely_high")
    assert crash and any("凭什么" in l for l in crash)
    assert c._template_lines_for("dere_dere", "meal") == []  # 防线融化未覆盖类别 → 空
    assert c._template_lines_for("playful_bubbly", "playful") == []  # 非 tsundere_*/dere → 空
    print("  OK test_template_lines_for")


def test_situation_contains_templates():
    """cue 选中时 compose_situation 输出包含台词示范"""
    c = make_composer()
    combo = {
        "size": 2,
        "intent": {"text": "随便问问", "tone": "casual"},
        "cue": {
            "name": "tsundere_classic",
            **c.CUES["tsundere_classic"],
            "templates": c._template_lines_for("tsundere_classic", "lonely_low"),
        },
        "vibe": None,
        "combo_string": "a_b",
    }
    situation = c.compose_situation(combo, None, 3.0)
    assert "风格指引" in situation and "台词示范" in situation
    print("  OK test_situation_contains_templates")


def test_select_combo_attaches_templates():
    """select_combo 选中 tsundere_* 时附带模板（随机验证，300 次内必中）"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    found = False
    for _ in range(300):
        combo = c.select_combo("lonely_low", now)
        cue = combo.get("cue")
        if cue and cue["name"] == "tsundere_classic":
            assert "templates" in cue
            assert cue["templates"], "tsundere_classic 应附带参考台词"
            found = True
            break
    assert found, "300 次内应至少选中一次 tsundere_classic"
    print("  OK test_select_combo_attaches_templates")


if __name__ == "__main__":
    print("test_toml_binding.py\n")
    tests = [
        test_toml_files_exist,
        test_meta_name_is_chiguo,
        test_cue_meta_by_cue_id,
        test_trigger_templates_loaded,
        test_template_lines_for,
        test_situation_contains_templates,
        test_select_combo_attaches_templates,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} toml-binding tests passed.")
