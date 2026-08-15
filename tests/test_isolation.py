#!/usr/bin/env python3
"""test_isolation.py — 隔离边界静态断言(§5.1/拷问 17,十轮审计 J):
引擎层模块(daemon/trigger/topics/composer)顶层不得 import schedule 包;
daemon 的 --attention/--schedule-recall/--schedule-change 分支函数体内惰性 import 为合法例外(按函数名排除)。"""

import ast, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_MODULES = ["chiguo_daemon.py", "chiguo_trigger.py", "chiguo_topics.py", "chiguo_composer.py"]
# daemon 惰性 import 合法例外(批 5 起):分支函数体内 import schedule 纯函数;
# main() 内 CLI 分支惰性 import(如 --anniversary/--break 的 ScheduleApi)同样只随 CLI 调用发生,不污染模块导入路径
DAEMON_EXEMPT_FUNCS = {"_cmd_schedule_change", "_cmd_attention", "_cmd_schedule_recall", "main"}


def _top_level_schedule_imports(tree):
    """顶层(非函数体)的 schedule import 列表;函数体内 import 仅豁免白名单函数(daemon 惰性分支)"""
    funcs = [(n.name, n.lineno, n.end_lineno or n.lineno) for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)]
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        names = [a.name for a in node.names]
        mod = getattr(node, "module", "") or (names[0] if names else "")
        if not (mod.startswith("schedule") or any(n.startswith("schedule") for n in names)):
            continue
        inside = [f for f in funcs if f[1] <= node.lineno <= f[2]]
        if inside and inside[-1][0] in DAEMON_EXEMPT_FUNCS:
            continue
        out.append((node.lineno, mod))
    return out


def test_engine_no_schedule_import():
    for fname in ENGINE_MODULES:
        tree = ast.parse(open(os.path.join(REPO, fname), encoding="utf-8").read())
        bad = _top_level_schedule_imports(tree)
        assert not bad, f"{fname} 顶层 import schedule: {bad}"
    print("  OK test_engine_no_schedule_import")


def test_state_is_only_bridge():
    """chiguo_state.py 是引擎侧唯一允许 import schedule 的模块(门面)"""
    tree = ast.parse(open(os.path.join(REPO, "chiguo_state.py"), encoding="utf-8").read())
    imps = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    mods = [getattr(n, "module", "") for n in imps]
    assert any(m and m.startswith("schedule") for m in mods), "chiguo_state 应经 schedule 纯函数"
    print("  OK test_state_is_only_bridge")



