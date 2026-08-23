"""state — 状态域聚合包（PR-2 重构，AUD-001/004/009/010）。"""
from state.ownership import _OWNER_PLACEHOLDER, _config_owner, _is_placeholder_owner, _check_owner_mismatch
from state.persistence import StatePersistence

__all__ = [
    "_OWNER_PLACEHOLDER", "_config_owner", "_is_placeholder_owner", "_check_owner_mismatch",
    "StatePersistence",
]
