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


def _cooldown_bare_field_violations(tree):
    """返回 (lineno, expr) 列表：形如 `X.state.cooldown.<field>` 且 field 非方法调用。"""
    bad = []
    # 收集所有被方法调用包裹的属性（其父节点是 Call 且是 func）→ 视为方法访问（合法）
    call_func_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Attribute):
                call_func_ids.add(id(node.func))

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


if __name__ == "__main__":
    print("test_state_private_access_guard.py\n")
    tests = [test_daemon_private_access_closure, test_state_access_closure_all_engine_modules]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
