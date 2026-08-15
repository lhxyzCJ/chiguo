#!/usr/bin/env python3
"""chiguo_paths — 项目根统一锚点。

T10·Q2 daemon 拆包后，各子包（decision/ runner/ cli/ ops/）不再以自身 __file__
定位项目根（子包在子目录，__file__ 指向子目录）。统一以 chiguo_version.py 所在
目录（仓库根）作唯一锚点，避免对 chiguo_proactive.toml / personality/ 的相对路径漂移。
"""
from pathlib import Path

import chiguo_version

PROJECT_ROOT = Path(chiguo_version.__file__).resolve().parent
