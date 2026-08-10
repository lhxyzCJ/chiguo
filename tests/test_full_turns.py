#!/usr/bin/env python3
"""test_full_turns.py — C4 写全对话轮次测试

覆盖: DecisionEngine._mem0_autowrite 在 [memory].write_full_turns 开启时
写入 user+assistant 两轮（assistant 文本取 recent_sent_texts），默认单条 user 恒等；
短消息跳过、bridge 不可用跳过、无最近发送文本时降级单条。
零 LLM、零网络（FakeBridge 注入，不触真实 mem0）。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine  # noqa: E402


class FakeBridge:
    """记忆桥替身：available=True，记录 add_messages 调用。"""

    available = True

    def __init__(self):
        self.add_calls = []
        self._unavailable = False

    def add_messages(self, messages, metadata=None):
        self.add_calls.append({"messages": messages, "metadata": metadata})
        return True


class _UnavailableBridge(FakeBridge):
    available = False


def _engine(mem_cfg: dict, bridge: FakeBridge, recent=None):
    eng = SimpleNamespace(
        config={"memory": mem_cfg},
        state=SimpleNamespace(memory_bridge=bridge),
    )
    eng.recent_sent_texts = lambda n=1: (list(recent) if recent else [])
    return eng


def _call_autowrite(eng, text: str):
    DecisionEngine._mem0_autowrite(eng, text)


def test_default_single_user_turn():
    """write_full_turns 默认 False → 只写一条 user（恒等）。"""
    bridge = FakeBridge()
    eng = _engine({"write_full_turns": False}, bridge, recent=["最近发过的话"])
    _call_autowrite(eng, "哥哥今天工作累吗？")
    assert len(bridge.add_calls) == 1
    msgs = bridge.add_calls[0]["messages"]
    assert msgs == [{"role": "user", "content": "哥哥今天工作累吗？"}]
    print("  OK test_default_single_user_turn")


def test_enabled_writes_user_and_assistant():
    """write_full_turns=True → 写 user+assistant 两轮，assistant 取最近发送文本。"""
    bridge = FakeBridge()
    eng = _engine({"write_full_turns": True}, bridge,
                  recent=["嗯……我才没有在等你。", "吃饭了吗"])
    _call_autowrite(eng, "哥哥今天工作累吗？")
    msgs = bridge.add_calls[0]["messages"]
    assert msgs == [
        {"role": "user", "content": "哥哥今天工作累吗？"},
        {"role": "assistant", "content": "嗯……我才没有在等你。"},
    ], msgs
    print("  OK test_enabled_writes_user_and_assistant")


def test_enabled_no_recent_single_turn():
    """write_full_turns=True 但无最近发送文本 → 降级单条 user（不崩）。"""
    bridge = FakeBridge()
    eng = _engine({"write_full_turns": True}, bridge, recent=[])
    _call_autowrite(eng, "哥哥今天工作累吗？")
    assert bridge.add_calls[0]["messages"] == [
        {"role": "user", "content": "哥哥今天工作累吗？"}
    ]
    print("  OK test_enabled_no_recent_single_turn")


def test_metadata_preserved():
    """metadata 基础键保留（category/scope/source），不受写全轮次影响。"""
    bridge = FakeBridge()
    eng = _engine({"write_full_turns": True}, bridge, recent=["回复文本"])
    _call_autowrite(eng, "哥哥今天工作累吗？")
    meta = bridge.add_calls[0]["metadata"]
    assert meta["category"] == "conversation"
    assert meta["scope"] == "global" and meta["source"] == "daemon"
    print("  OK test_metadata_preserved")


def test_short_message_skipped():
    """短消息（<8 字）→ 不写（寒暄/无信息量跳过）。"""
    bridge = FakeBridge()
    eng = _engine({"write_full_turns": True}, bridge, recent=["回复"])
    _call_autowrite(eng, "在吗")
    assert bridge.add_calls == []
    print("  OK test_short_message_skipped")


def test_unavailable_bridge_skipped():
    """bridge 不可用 → 不写、不抛。"""
    bridge = _UnavailableBridge()
    eng = _engine({"write_full_turns": True}, bridge, recent=["回复"])
    _call_autowrite(eng, "哥哥今天工作累吗？")
    assert bridge.add_calls == []
    print("  OK test_unavailable_bridge_skipped")


def test_config_missing_defaults_false():
    """config 无 memory 段 → 默认单条 user（不抛）。"""
    eng = SimpleNamespace(
        config={},
        state=SimpleNamespace(memory_bridge=FakeBridge()),
    )
    eng.recent_sent_texts = lambda n=1: ["最近发过的话"]
    _call_autowrite(eng, "哥哥今天工作累吗？")
    assert eng.state.memory_bridge.add_calls[0]["messages"] == [
        {"role": "user", "content": "哥哥今天工作累吗？"}
    ]
    print("  OK test_config_missing_defaults_false")


if __name__ == "__main__":
    print("test_full_turns.py\n")
    tests = [
        test_default_single_user_turn,
        test_enabled_writes_user_and_assistant,
        test_enabled_no_recent_single_turn,
        test_metadata_preserved,
        test_short_message_skipped,
        test_unavailable_bridge_skipped,
        test_config_missing_defaults_false,
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
    print(f"\n{'=' * 40}")
    total = len(tests)
    print(f"ALL {total} full-turns tests, {total - failed} passed, {failed} failed.")
    sys.exit(1 if failed else 0)
