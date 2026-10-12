#!/usr/bin/env python3
"""chiguo_paths — 项目根统一锚点。

以本文件所在目录（仓库根）为唯一锚点，避免对 chiguo_proactive.toml /
personality/ 的相对路径漂移。子包一律 `from chiguo_paths import PROJECT_ROOT`，
不要用自身的 __file__——子包在子目录，那里的 __file__ 指向子目录而非仓库根。
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
