"""ops.analysis_ops — record_user_message 核心分析逻辑（AUD-003）。"""

import hashlib
import json
import sys
from datetime import datetime
from chiguo_time import CST


_MEM0_AUTOWRITE_DEDUP_HOURS = 24.0

def _mem0_autowrite_hashes_dict(self) -> dict:
    d = getattr(self, "_mem0_autowrite_hashes", None)
    if d is None:
        d = {}
        self._mem0_autowrite_hashes = d
    return d

def _mem0_autowrite_deduped(self, text: str) -> bool:
    if not text.strip():
        return False
    h = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    now = datetime.now(CST)
    d = _mem0_autowrite_hashes_dict(self)
    stale = []
    for k, iso in list(d.items()):
        try:
            ts = datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            stale.append(k)
            continue
        if (now - ts).total_seconds() > _MEM0_AUTOWRITE_DEDUP_HOURS * 3600:
            stale.append(k)
    for k in stale:
        d.pop(k, None)
    prev = d.get(h)
    if prev:
        try:
            return (now - datetime.fromisoformat(prev)).total_seconds() <= _MEM0_AUTOWRITE_DEDUP_HOURS * 3600
        except (TypeError, ValueError):
            return False
    return False

def _mem0_autowrite_record(self, text: str):
    if not text.strip():
        return
    h = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    _mem0_autowrite_hashes_dict(self)[h] = datetime.now(CST).isoformat()

def parse_analysis_json(analysis_json: str | None) -> dict | None:
    if not analysis_json:
        return None
    try:
        d = json.loads(analysis_json)
        if not isinstance(d, dict):
            raise ValueError("analysis is not a dict")
        return d
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        print(f"[warn] 分析JSON解析失败: {e}，降级为纯长度模式", file=sys.stderr)
        return None

def is_recv_upgrade(analysis_dict, dedup, text_sha: str, recv_id, now: datetime, window_s: float) -> bool:
    is_upgrade = (
        analysis_dict is not None
        and bool(dedup)
        and not dedup.get("analysis")
    )
    if is_upgrade:
        if recv_id and dedup.get("recv_id") == recv_id:
            is_upgrade = True
        elif dedup.get("text_sha") == text_sha and dedup.get("at"):
            try:
                prev_at = datetime.fromisoformat(dedup["at"])
                is_upgrade = (now - prev_at).total_seconds() < window_s
            except (ValueError, TypeError):
                is_upgrade = False
        else:
            is_upgrade = False
    return is_upgrade
