# decision_schema.py — 迟菓决策 JSON 集中 schema + 契约版本键（Q16）
"""决策 JSON 的单一 schema 定义。

背景（Q16）：daemon 此前在多处内联构造决策 dict（send/idle/recv/recv_upgrade/
send_result），跨语言消费者（python monitor、node agent-run）只能人肉对齐字段名。
本模块收拢结构/必填字段/类型，提供 validate()，并在顶层加契约版本键 `contract`。

- 契约版本键 `contract`：与项目版本（chiguo_version.VERSION）分离。历史 jsonl
  （无此键）在读取时按缺省契约 1 处理（向后兼容，不破坏读取）。
- 消费方互引：Python 侧（daemon 写前校验、monitor 读校验）以这里为准；
  node scripts/agent-run.mjs 无法 import Python schema，仅对齐字段名清单，
  见 agent-run.mjs 中互引注释（keep in sync：agent-run.mjs 顶部 DECISION 引用块）
"""

# 决策 JSON 顶层契约版本键值。历史 jsonl（无此键）读取时按缺省 1 处理。
CONTRACT = 1

# 合法 action 枚举（决策日志写入的 5 类记录；consolidate 为 CLI 输出不经 _log）。
ACTIONS = ("send", "idle", "recv", "recv_upgrade", "send_result")

# send_result.status 枚举
SEND_RESULT_STATUS = ("success", "failed")

# send_result 幂等去重标志要求 bool
# 各 action 的稳定顶端字段约束：{字段: 类型别名}。
# 仅约束顶端稳定字段；context/state 为自由 dict（内部结构由各自提供方约束）。
# 类型别名 → 判定函数映射（_TYPES）。
_STRING = "str"
_DICT = "dict"
_BOOL = "bool"
_NUMBER = "number"  # int/float，不含 bool
_NONE_OR_STR = "none_or_str"

# 每 action：required（必填+类型）与 optional（可选+类型，缺失视为合法）。
_REQUIRED = {
    "send": {
        "action": _STRING,
        "contract": lambda _: True,  # 由 daemon 写入；历史读取时不强制（见 validate）
        "version": _STRING,
        "msg_id": _STRING,
        "trigger": _STRING,
        "intensity": _STRING,
        "context": _DICT,
        "state": _DICT,
    },
    "idle": {
        "action": _STRING,
        "version": _STRING,
        "reason": _STRING,
        "state": _DICT,
    },
    "recv": {
        "action": _STRING,
        "msg_id": _STRING,
        "message_text": _STRING,
        "message_length": _NUMBER,
        "state": _DICT,
    },
    "recv_upgrade": {
        "action": _STRING,
        "msg_id": _STRING,
        "message_text": _STRING,
        "user_emotion_analysis": _DICT,
        "state": _DICT,
    },
    "send_result": {
        "action": _STRING,
        "msg_id": _STRING,
        "status": lambda v: v in SEND_RESULT_STATUS,
        "error": _NONE_OR_STR,
        "time": _STRING,
        "refunded": _BOOL,
        "duplicate": _BOOL,
    },
}

# 可选字段（缺失合法，出现则须类型匹配）
_OPTIONAL = {
    "send": {
        "data_warning": _BOOL,
        "bayesian": _DICT,
        "contract": lambda v: v == CONTRACT,
        "next_evaluation_at": _STRING,
        "state": _DICT,
    },
    "idle": {
        "data_warning": _BOOL,
        "bayesian": _DICT,
        "contract": lambda v: v == CONTRACT,
        "next_evaluation_at": _STRING,
        "state": _DICT,
    },
    "recv": {
        "user_emotion_analysis": _DICT,
        "contract": lambda v: v == CONTRACT,
        "data_warning": _BOOL,
        "state": _DICT,
    },
    "recv_upgrade": {
        "contract": lambda v: v == CONTRACT,
        "data_warning": _BOOL,
        "state": _DICT,
    },
    "send_result": {
        "contract": lambda v: v == CONTRACT,
        "data_warning": _BOOL,
        "state": _DICT,
    },
}


def _is_dict(v) -> bool:
    return isinstance(v, dict)


def _is_str(v) -> bool:
    return isinstance(v, str)


def _is_bool(v) -> bool:
    return isinstance(v, bool)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_none_or_str(v) -> bool:
    return v is None or isinstance(v, str)


_TYPES = {
    _STRING: _is_str,
    _DICT: _is_dict,
    _BOOL: _is_bool,
    _NUMBER: _is_number,
    _NONE_OR_STR: _is_none_or_str,
}


def _check_type(field: str, value, type_ref) -> str | None:
    """返回字段类型错误描述（合法返回 None）。type_ref 可为类型别名或谓词函数。"""
    if callable(type_ref):
        if type_ref(value):
            return None
        return f"{field!r} 值不满足约束（坏类型/非法枚举）: {value!r}"
    check = _TYPES.get(type_ref)
    if check is None:
        return None  # 未知类型别名 → 不做约束
    if check(value):
        return None
    return f"{field!r} 类型应为 {type_ref}，实得 {type(value).__name__}"


def validate(decision: dict, *, require_contract: bool = False) -> list[str]:
    """校验一条决策 JSON，返回错误清单（空 list = 合法）。

    require_contract=True 时强制要求顶层 `contract` 键存在（daemon 写前校验用）；
    默认 False（monitor 读历史 jsonl 时旧记录无 contract 键 → 按缺省 1 处理，
    不因缺失判非法）。
    """
    errors: list[str] = []
    if not isinstance(decision, dict):
        return ["决策不是 dict 对象"]
    action = decision.get("action")
    if not isinstance(action, str):
        return ["action 缺失或非字符串"]
    if action not in ACTIONS:
        return [f"action {action!r} 不在合法枚举 {list(ACTIONS)} 内"]

    if require_contract:
        if "contract" not in decision:
            errors.append("缺少契约版本键 'contract'")
        elif decision["contract"] != CONTRACT:
            errors.append(f"contract 应为 {CONTRACT}，实得 {decision['contract']!r}")
    # 缺省 merge：历史无 contract → 视为 CONTRACT（向后兼容）
    if "contract" not in decision:
        merged = dict(decision, contract=CONTRACT)
    else:
        merged = decision

    for field, type_ref in _REQUIRED[action].items():
        if field == "contract":
            continue  # 已单独处理
        if field not in merged:
            errors.append(f"{action} 缺失必填字段 {field!r}")
            continue
        err = _check_type(field, merged[field], type_ref)
        if err:
            errors.append(err)

    for field, type_ref in _OPTIONAL[action].items():
        if field == "contract":
            continue
        if field in merged and merged[field] is not None:
            err = _check_type(field, merged[field], type_ref)
            if err:
                errors.append(err)
    # contract 类型校验（出现在 merged 则必须等于 CONTRACT）
    if "contract" in merged:
        err = _check_type("contract", merged["contract"], lambda v: v == CONTRACT)
        if err:
            errors.append(err)

    # send_result 特有跨字段：status 枚举已由 required 处理；refunded/duplicate 类型已处理
    return errors


def with_contract(decision: dict) -> dict:
    """返回带顶层 contract 键的决策副本（已是 CONTRACT 则不重复加）。"""
    if decision.get("contract") == CONTRACT:
        return decision
    return {**decision, "contract": CONTRACT}


def send_top_level_fields() -> list[str]:
    """send 记录的顶层字段名清单（required ∪ optional，排序）——node 侧契约测试对齐用。

    mjs（scripts/agent-run.mjs）无法 import Python schema，tests/test_agent_run.mjs
    读取本函数结果（经子进程执行脚本），与 agent-run.mjs 的 DECISION_SEND_FIELDS
    互检，确保跨语言字段名不漂移。
    """
    return sorted(set(_REQUIRED["send"]) | set(_OPTIONAL["send"]))


if __name__ == "__main__":
    # 独立自检（无 pytest）：svn 风格最小断言
    samples = [
        {"action": "send", "version": "1.19", "msg_id": "m1",
         "trigger": "lonely_mid", "intensity": "soft",
         "context": {"layer": "shell"}, "state": {}},
        {"action": "idle", "version": "1.19", "reason": "no_trigger", "state": {}},
        {"action": "recv", "msg_id": "m2", "message_text": "hi",
         "message_length": 2, "state": {}},
        {"action": "recv_upgrade", "msg_id": "m3", "message_text": "hi",
         "user_emotion_analysis": {"warmth": 0.5}, "state": {}},
        {"action": "send_result", "msg_id": "m4", "status": "failed",
         "error": None, "time": "2026-08-15 10:00", "refunded": True, "duplicate": False},
    ]
    for s in samples:
        errs = validate(with_contract(s), require_contract=True)
        if errs:
            raise SystemExit(f"自检失败: {s} -> {errs}")
    # 非法样例
    bad = {"action": "send", "version": "1.19", "msg_id": "m1",
           "context": {"layer": "shell"}, "state": {}}  # 缺 trigger/intensity
    if not validate(bad):
        raise SystemExit("自检失败: 缺 trigger/intensity 不应通过")
    bad2 = {"action": "send_result", "msg_id": "m4", "status": "meh",
            "error": None, "time": "t", "refunded": True, "duplicate": False}
    if not validate(bad2):
        raise SystemExit("自检失败: send_result 非法 status 不应通过")
    # 历史兼容：无 contract → 缺省 1 合法
    legacy = {"action": "idle", "version": "1.19", "reason": "x", "state": {}}
    if validate(legacy):
        raise SystemExit("自检失败: 无 contract 缺省应合法")
    print("decision_schema 自检通过")
