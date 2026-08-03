# ============================================================
# schedule/plan_store.py — 计划文件存储(v1,LLM 产物,仅 modifiers + ref)
# 读损坏/缺失 → None(引擎恒等 1.0,spec §5.2);写 = 原子写 0600。
# 唯一写方 = replan(Task 15);引擎永不写。
# ============================================================

import json
import os
import sys
from pathlib import Path


class PlanStore:
    def __init__(self, base_dir: str):
        self._path = Path(base_dir) / "schedule_plan.json"

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text())
            if not isinstance(data, dict) or "modifiers" not in data:
                raise ValueError("plan 缺 modifiers")
            return data
        except (json.JSONDecodeError, ValueError, OSError):
            print(f"[schedule.plan_store] schedule_plan.json 损坏,按缺失处理: {self._path}",
                  file=sys.stderr)
            return None

    def save(self, plan: dict) -> None:
        tmp = Path(str(self._path) + ".tmp")
        tmp.write_text(json.dumps(plan, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def exists(self) -> bool:
        return self._path.exists()

    def generated_at(self) -> str | None:
        p = self.load()
        return p.get("generated_at") if p else None
