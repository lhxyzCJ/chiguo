#!/usr/bin/env python3
"""test_chiguo_version.py — chiguo_version 版本一致性测试 (U8, Issue #230)

校验点:
  1. VERSION 格式: MAJOR.MINOR 次版本计数（非十进制加法的数值，1.9 -> 1.10）
  2. 引用一致性: envcheck / monitor / daemon 三者均 import VERSION；
     且 envcheck / monitor 报告以 app_version 键承载 == VERSION
  3. chiguo_version.py 文档明示单一来源 + 步进规则
""".strip()

import importlib
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from chiguo_version import VERSION

def test_version_format():
    """VERSION 为 MAJOR.MINOR 次版本计数格式（如 1.10），非十进制加法。"""
    assert re.fullmatch(r"[1-9]\d*\.\d+", VERSION), f"VERSION={VERSION!r} 非法格式"
    major, minor = VERSION.split(".", 1)
    assert major.isdigit(), "MAJOR 非数字"
    # 次版本是计数递增标识符，允许非十进制进位（1.9 -> 1.10），故 minor 可为多位数
    assert minor.isdigit(), "MINOR 非数字"
    print(f"  OK test_version_format (VERSION={VERSION})")

def test_version_single_source_docstring():
    """chiguo_version.py 为单一来源，docstring 声明步进规则。"""
    src = (ROOT / "chiguo_version.py").read_text(encoding="utf-8")
    assert "VERSION = " in src
    assert "单一来源" in src or "single source" in src.lower()
    assert "步进规则" in src or "MINOR + 1" in src
    print("  OK test_version_single_source_docstring")

def test_consumers_import_version():
    """envcheck / monitor / daemon 三消费方均从 chiguo_version import VERSION。"""
    for fname in ("chiguo_envcheck.py", "chiguo_monitor.py", "chiguo_daemon.py"):
        src = (ROOT / fname).read_text(encoding="utf-8")
        assert re.search(r"from chiguo_version import \bVERSION\b", src), f"{fname} 未 import VERSION"
        print(f"  OK test_consumers_import_version ({fname})")

def test_envcheck_monitor_app_version():
    """envcheck / monitor 报告以 app_version 键承载 VERSION（取值即 VERSION）。"""
    for fname in ("chiguo_envcheck.py", "chiguo_monitor.py"):
        src = (ROOT / fname).read_text(encoding="utf-8")
        assert "app_version" in src, f"{fname} 报告缺 app_version 键"
        assert re.search(r'"app_version"\s*:\s*VERSION', src), \
            f"{fname} app_version 键未直接承载 VERSION"
        print(f"  OK test_envcheck_monitor_app_version ({fname})")

def test_consumer_modules_loadable():
    """各消费模块运行时能正常导入（chiguo_version 受损时不会在 import 阶段炸）。"""
    for mod in ("chiguo_envcheck", "chiguo_monitor"):
        assert importlib.import_module(mod) is not None, f"import {mod} 失败"
    print("  OK test_consumer_modules_loadable")



