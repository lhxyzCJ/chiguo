"""state.ownership — owner 分区守卫（AUD-004/009）。"""

_OWNER_PLACEHOLDER = "owner@im.wechat"


def _config_owner(cfg: dict) -> str | None:
    if not isinstance(cfg, dict):
        return None
    if "owner" in cfg and cfg["owner"]:
        v = cfg["owner"]
        if isinstance(v, str) and v.strip():
            return v.strip()
    w = cfg.get("wechat")
    if isinstance(w, dict):
        for k in ("wechat_recipient", "owner"):
            v = w.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    v = cfg.get("wechat_recipient")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _is_placeholder_owner(v: str | None) -> bool:
    return not v or v == _OWNER_PLACEHOLDER


def _check_owner_mismatch(config_owner: str | None, disk_owner: str | None) -> bool:
    if _is_placeholder_owner(config_owner) or _is_placeholder_owner(disk_owner):
        return False
    return config_owner != disk_owner
