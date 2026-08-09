#!/usr/bin/env python3
"""test_composer_fallback.py — A8 生成失败确定性回退 CLI 冒烟测试

覆盖：decision 文件成功/缺 trigger/不可读、--trigger 模式、无参数报错、
模板行号注释剥离、无模板时 intent 兜底。
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_composer import _cli_main, _fallback_text


def _run(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli_main(argv)
    return rc, buf.getvalue()


def test_cli_decision_file_success():
    """decision JSON 文件（含 trigger + state.emotion 快照）→ RC 0 + 非空文本"""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "decision.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"action": "send", "msg_id": "m1", "trigger": "lonely_mid",
                       "state": {"emotion": {"loneliness": 70, "anxiety": 30}}}, f)
        rc, out = _run([path])
        assert rc == 0, f"RC 期望 0 实得 {rc}"
        assert out.strip(), "应输出可发送文本"
    print(f"  OK test_cli_decision_file_success: {out.strip()[:40]!r}")


def test_cli_decision_file_missing_trigger():
    """decision JSON 缺 trigger → RC 1（失败非零）"""
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "decision.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"action": "send"}, f)
        rc, out = _run([path])
        assert rc == 1, f"RC 期望 1 实得 {rc}"
    print("  OK test_cli_decision_file_missing_trigger")


def test_cli_decision_file_unreadable():
    """文件不存在 → RC 1（失败非零）"""
    rc, out = _run(["/nonexistent/decision.json"])
    assert rc == 1, f"RC 期望 1 实得 {rc}"
    print("  OK test_cli_decision_file_unreadable")


def test_cli_trigger_flag():
    """--trigger 直传模式 → RC 0 + 非空文本"""
    rc, out = _run(["--trigger", "night"])
    assert rc == 0 and out.strip(), f"RC={rc} out={out!r}"
    print(f"  OK test_cli_trigger_flag: {out.strip()[:40]!r}")


def test_cli_no_args_errors():
    """无参数 → parser.error → SystemExit(2)"""
    raised = False
    try:
        _run([])
    except SystemExit as e:
        raised = True
        assert e.code == 2, f"期望退出码 2 实得 {e.code}"
    assert raised, "无参数应 parser.error → SystemExit(2)"
    print("  OK test_cli_no_args_errors")


def test_fallback_text_strips_line_annotations():
    """模板行尾的行号/风格注释（（L1069 …））被剥离 → 可发送文本"""
    combo = {"cue": {"templates": [
        "嗯嗯。一个鸡肉三明治～（L1069 报单风早安）",
        "……不告诉你。（L10856）",
    ]}}
    text = _fallback_text(combo)
    assert "（L" not in text and "(L" not in text, text
    assert "嗯嗯。一个鸡肉三明治～" in text, text
    assert "……不告诉你。" in text, text
    print(f"  OK test_fallback_text_strips_line_annotations: {text!r}")


def test_fallback_text_intent_fallback():
    """无 cue 模板（size=1）→ 固定可发送文案池兜底（不直发 LLM 意图指示）；行数 ≤3"""
    from chiguo_composer import _FALLBACK_LINES
    combo = {"intent": {"text": "傲娇提醒——暗示哥哥太久没联系但不明说"}}
    out = _fallback_text(combo)
    assert out in _FALLBACK_LINES, f"intent 兜底必须来自文案池, got {out!r}"
    many = {"cue": {"templates": [f"模板{i}" for i in range(5)]}}
    assert len(_fallback_text(many).split("\n")) == 3, "最多 3 句"
    print("  OK test_fallback_text_intent_fallback")


if __name__ == "__main__":
    print("test_composer_fallback.py\n")
    tests = [
        test_cli_decision_file_success,
        test_cli_decision_file_missing_trigger,
        test_cli_decision_file_unreadable,
        test_cli_trigger_flag,
        test_cli_no_args_errors,
        test_fallback_text_strips_line_annotations,
        test_fallback_text_intent_fallback,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    total = len(tests)
    passed = total - failed
    print(f"ALL {total} tests, {passed} passed, {failed} failed.")
    if failed:
        sys.exit(1)
    sys.exit(0)
