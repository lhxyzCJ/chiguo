# ============================================================
# chiguo_auth.py — 统一的 pi provider key 读取
# 抽离 chiguo_envcheck._pi_api_key 与 memory/mem0_backend._pi_api_key 的重复实现
# 单一来源：~/.pi/agent/auth.json 的 opencode-go 条目
# ============================================================

import json
import os


def pi_api_key(provider: str = "opencode-go") -> str | None:
    """从 ~/.pi/agent/auth.json 读 provider 的 API key；失败返回 None。"""
    try:
        with open(os.path.expanduser("~/.pi/agent/auth.json"), encoding="utf-8") as f:
            return (json.load(f).get(provider) or {}).get("key") or None
    except Exception:
        return None


# 兼容旧名：保留 _pi_api_key 别名供渐进迁移
_pi_api_key = pi_api_key
