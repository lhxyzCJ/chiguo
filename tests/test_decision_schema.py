#!/usr/bin/env python3
"""test_decision_schema.py — 决策 JSON 集中 schema 校验测试（Q16）

覆盖：合法/非法决策 JSON 断言、契约版本键 contract、历史 jsonl 兼容
（无 contract → 缺省 1 合法）、daemon._log 写前统一加 contract 并校验非法抛错。
"""

import json
import os
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision_schema import (  # noqa: E402
    CONTRACT, ACTIONS, validate, with_contract, send_top_level_fields,
)
from chiguo_daemon import DecisionEngine  # noqa: E402

_GOOD_SEND = {
    "action": "send", "version": "1.19", "msg_id": "m1",
    "trigger": "lonely_mid", "intensity": "soft",
    "context": {"layer": "shell"}, "state": {},
}
_GOOD_IDLE = {"action": "idle", "version": "1.19", "reason": "no_trigger", "state": {}}
_GOOD_RECV = {"action": "recv", "msg_id": "m2", "message_text": "hi",
              "message_length": 2, "state": {}}
_GOOD_UPGRADE = {"action": "recv_upgrade", "msg_id": "m3", "message_text": "hi",
                 "user_emotion_analysis": {"warmth": 0.5}, "state": {}}
_GOOD_SEND_RESULT = {"action": "send_result", "msg_id": "m4", "status": "failed",
                     "error": None, "time": "2026-08-15 10:00",
                     "refunded": True, "duplicate": False}


def test_valid_records():
    for good in (_GOOD_SEND, _GOOD_IDLE, _GOOD_RECV, _GOOD_UPGRADE, _GOOD_SEND_RESULT):
        assert validate(with_contract(good), require_contract=True) == [], good
        # 原记录无 contract → 亦合法（历史兼容）
        assert validate(good) == [], good
    print("  OK test_valid_records")


def test_actions_enum():
    assert "send" in ACTIONS and "idle" in ACTIONS
    assert "recv" in ACTIONS and "recv_upgrade" in ACTIONS and "send_result" in ACTIONS
    # 未知 action 判非法
    bad = {"action": "nope"}
    errs = validate(bad)
    assert errs, errs
    assert any("action" in e for e in errs), errs
    print("  OK test_actions_enum")


def test_invalid_records():
    # 缺必填字段
    missing_trigger = {k: v for k, v in _GOOD_SEND.items() if k != "trigger"}
    assert validate(missing_trigger), "缺 trigger 应非法"
    # 类型错误：context 非 dict
    wrong_type = dict(_GOOD_SEND, context="not-a-dict")
    assert validate(wrong_type), "context 非 dict 应非法"
    # send_result 非法 status 枚举
    bad_status = dict(_GOOD_SEND_RESULT, status="meh")
    assert validate(bad_status), "非法 status 应非法"
    # send_result refunded 非 bool
    bad_bool = dict(_GOOD_SEND_RESULT, refunded="yes")
    assert validate(bad_bool), "refunded 非 bool 应非法"
    # contract 值错误
    bad_contract = with_contract(_GOOD_SEND)
    bad_contract["contract"] = 999
    assert validate(bad_contract, require_contract=True), "错误 contract 应非法"
    print("  OK test_invalid_records")


def test_require_contract():
    # require_contract 强制要 contract 键
    errs = validate(_GOOD_SEND, require_contract=True)
    assert errs and any("contract" in e for e in errs), errs
    assert validate(with_contract(_GOOD_SEND), require_contract=True) == []
    print("  OK test_require_contract")


def test_with_contract_idempotent():
    once = with_contract(_GOOD_SEND)
    assert once["contract"] == CONTRACT
    again = with_contract(once)
    assert again["contract"] == CONTRACT
    assert again is once  # 已是 CONTRACT 不复制
    print("  OK test_with_contract_idempotent")


def test_send_top_level_fields_stable():
    fields = send_top_level_fields()
    for f in ("action", "contract", "version", "msg_id", "trigger",
              "intensity", "context", "state"):
        assert f in fields, f
    assert "no_such_field" not in fields
    print("  OK test_send_top_level_fields_stable")


def test_daemon_log_adds_contract_and_validates():
    """DecisionEngine._log：写前统一加 contract + 校验；非法记录抛 ValueError。"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Path("chiguo_proactive.toml").read_text()
        cfg_path = Path(tmp) / "chiguo_proactive.toml"
        cfg_path.write_text(cfg)
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        log_path = Path(tmp) / "decisions.jsonl"
        eng = DecisionEngine(str(cfg_path), str(log_path))
        log_file = eng.log_path

        eng._log(dict(_GOOD_SEND))
        entries = [json.loads(line) for line in Path(log_file).read_text().splitlines()]
        assert entries and entries[0]["action"] == "send"
        assert entries[0]["contract"] == CONTRACT, entries[0]
        assert validate(entries[0], require_contract=True) == []

        # 无 action 字段 → 写前抛错
        try:
            eng._log({"action": "bogus"})
            raise AssertionError("非法 action 应抛 ValueError")
        except ValueError:
            pass
    print("  OK test_daemon_log_adds_contract_and_validates")


def test_monitor_reads_historical_without_contract():
    """monitor 读取无 contract 的历史 jsonl → 不破坏（缺省 1）。"""
    from chiguo_monitor import ChiguoMonitor
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "decisions.jsonl"
        log.write_text(
            json.dumps(_GOOD_SEND) + "\n" +
            json.dumps(_GOOD_IDLE) + "\n",
            encoding="utf-8",
        )
        mon = ChiguoMonitor(log_path=str(log), state_path=str(Path(tmp) / "nope.json"),
                            config_path="chiguo_proactive.toml")
        rows = list(mon._iter_decisions())
        assert len(rows) == 2, rows
        actions = [r["action"] for r in rows]
        assert actions == ["send", "idle"], actions
    print("  OK test_monitor_reads_historical_without_contract")
