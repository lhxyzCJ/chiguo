#!/usr/bin/env python3
"""tests/_loop_worker.py — 并发一致性测试辅助进程（注入临时 toml，绝不碰真实仓库状态）。
用法: uv run python tests/_loop_worker.py <toml> <evaluate|record> [text]
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine


def main():
    cfg_path, action = sys.argv[1], sys.argv[2]
    text = sys.argv[3] if len(sys.argv) > 3 else "并发消息"
    engine = DecisionEngine(str(cfg_path))
    if action == "evaluate":
        engine.evaluate()
    else:
        engine.record_user_message(text)
    engine.state.save()
    print("ok")


if __name__ == "__main__":
    main()
