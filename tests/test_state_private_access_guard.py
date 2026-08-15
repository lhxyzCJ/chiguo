#!/usr/bin/env python3
"""test_state_private_access_guard.py — T11·Q1「私有访问收口」回归守卫。

断言 chiguo_daemon.py 等外部调用方不再直接触碰 ChiguoState / CooldownState 的私有成员，
也不对 cooldown 字段做裸字段读写（必须走公开 getter/mutator 方法）。

覆盖两类违规：
  A. 直访 state 私有成员：`x.state._foo(...)` / `x.state._foo = ...`（下划线开头）
  B. 直读写 cooldown 字段：`x.state.cooldown.<field>`（field 不是带 `(` 的方法调用）

不校验注释/字符串（用 AST 解析），避免误报文档中提及私有名的段落。
"""

import ast, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 被守卫（必须不直访 state 私有成员 / 不做 cooldown 裸字段读写）的外部调用方
GUARDED_MODULES = ["chiguo_daemon.py", "chiguo_trigger.py", "chiguo_topics.py", "chiguo_demo.py"]
# 允许不经公开 API 的私有访问名单（无；守卫目标是"零直访"）
ALLOWED_PRIVATE = set()

# CooldownState 的裸字段名（dataclass 定义）。守卫据此识别 getattr(x, 'field') 字符串字段形式。
COOLDOWN_FIELDS = {
    "last_message_at", "last_user_message_at", "messages_today", "messages_without_reply",
    "current_date", "morning_sent", "night_sent", "trigger_history", "event_timestamps",
    "reply_latencies", "busy_suppress_until", "held_count", "accumulated_lambda",
    "last_user_msg_length", "last_crash_at", "crash_count_48h", "crash_timestamps",
    "last_longing_break_at", "recv_dedup", "drop_events", "user_mood", "reply_stats",
    "reply_pending", "consolidate_last_at",
}


def _state_private_violations(tree):
    """返回 (lineno, expr) 列表：形如 `X.state._attr` 的访问。"""
    bad = []
    for node in ast.walk(tree):
        # 只关心取属性节点
        if not isinstance(node, ast.Attribute):
            continue
        attr_name = node.attr
        # 需要形如 `<X>.state` 的属性链，取其上一层 `._attr`
        val = node.value
        if not isinstance(val, ast.Attribute):
            continue
        if val.attr != "state":
            continue
        if not attr_name.startswith("_"):
            continue
        expr = f"{ast.unparse(val.value)}.{val.attr}.{attr_name}"
        bad.append((node.lineno, expr))
    return bad


def _is_state_cooldown_chain(node):
    """判断 ast 节点是否为 `(...state).cooldown` 链（node 属性名 == 'cooldown'）。

    base 可为变量 `state`（Name）或 `X.state`（Attribute）——即同时覆盖
    `getattr(state.cooldown, …)` 与 `getattr(x.state.cooldown, …)`。
    """
    if not isinstance(node, ast.Attribute) or node.attr != "cooldown":
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id == "state"
    return isinstance(base, ast.Attribute) and base.attr == "state"


def _cooldown_getattr_violations(tree):
    """返回 (lineno, expr) 列表：`getattr(<state>.cooldown, '<字段名>')` 字符串裸字段形式。

    字段名须为常量字符串且属于 COOLDOWN_FIELDS（裸字段读）；动态/方法名不命中。
    此为点式 `state.cooldown.field` 之外的第二类裸读（getattr 字符串形式）。
    """
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "getattr"):
            continue
        if len(node.args) < 2:
            continue
        obj, name_arg = node.args[0], node.args[1]
        if not _is_state_cooldown_chain(obj):
            continue
        field = None
        if isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str):
            field = name_arg.value
        if field is None or field not in COOLDOWN_FIELDS:
            continue
        expr = f"getattr(<state>.cooldown, '{field}')"
        bad.append((node.lineno, expr))
    return bad


def _cooldown_bare_field_violations(tree):
    """返回 (lineno, expr) 列表：cooldown 裸字段直读写（点式 + getattr 字符串形式）。"""
    # 收集所有被方法调用包裹的属性（其父节点是 Call 且是 func）→ 视为方法访问（合法）
    call_func_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                call_func_ids.add(id(node.func))

    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # 三级链：A.attr = <field>；A.value = B；B.attr == 'cooldown'；B.value = C；C.attr == 'state'
        field_name = node.attr
        b = node.value
        if not isinstance(b, ast.Attribute) or b.attr != "cooldown":
            continue
        c = b.value
        if not isinstance(c, ast.Attribute) or c.attr != "state":
            continue
        # 若该属性是某个方法调用的 func（即 `cooldown.some_method(`），合法
        if id(node) in call_func_ids:
            continue
        expr = f"<state>.cooldown.{field_name}"
        bad.append((node.lineno, expr))

    bad += _cooldown_getattr_violations(tree)
    return bad


def _assert_module_clean(fname):
    with open(os.path.join(REPO, fname), "r", encoding="utf-8") as f:
        src = f.read()
    tree = ast.parse(src)
    priv = _state_private_violations(tree)
    bare = _cooldown_bare_field_violations(tree)
    assert not priv, f"{fname} 直访 state 私有成员: {priv}"
    assert not bare, f"{fname} 直读写 cooldown 字段（应走公开方法）: {bare}"


def test_daemon_private_access_closure():
    _assert_module_clean("chiguo_daemon.py")
    print("  OK test_daemon_private_access_closure")


def test_state_access_closure_all_engine_modules():
    for fname in GUARDED_MODULES:
        if fname == "chiguo_daemon.py":
            continue
        _assert_module_clean(fname)
    print("  OK test_state_access_closure_all_engine_modules")
