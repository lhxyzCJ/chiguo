# ============================================================
# schedule/override_store.py — 临时例外/区间事实/提醒日存储(v1)
# 唯一写入口 = schedule/api.py;读路径(day_plan/T1/recall)零写。
# 损坏策略(spec §12.3):读返回空集 + stderr 告警,不落盘;重建归 api 迁移入口。
# ============================================================

import json
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
OVERRIDE_VERSION = 1
KINDS = ("cancel", "move", "add", "exam_week", "reminder")
MAX_FIELD_LEN = 100      # label/note/course 名 ≤ 100 字节
MAX_ITEM_BYTES = 4096    # item JSON 总长 ≤ 4KB


class OverrideError(ValueError):
    """校验失败(值级/形态/互斥)。api 层捕获并映射 ApiRejection。"""


class OverrideStore:
    def __init__(self, base_dir: str):
        self._path = Path(base_dir) / "schedule_overrides.json"
        self._items: list[dict] = []
        self._corrupt = False
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def corrupt(self) -> bool:
        return self._corrupt

    def _load(self):
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, TypeError, OSError):
            self._items = []
            self._corrupt = True
            print(f"[schedule.override_store] schedule_overrides.json 损坏,读为空集: {self._path}",
                  file=sys.stderr)
            return
        # 字段级校验:逐条 kind 枚举 + ISO 日期,坏条目剔除并置 corrupt(防读路径崩溃)
        items, bad = [], 0
        for it in data.get("items", []) if isinstance(data, dict) else []:
            if not isinstance(it, dict) or not it.get("id"):
                bad += 1
                continue
            if it.get("kind") not in KINDS:
                bad += 1
                continue
            ok = True
            for key in ("date", "end_date", "to_date"):
                v = it.get(key)
                if v is not None:
                    try:
                        date.fromisoformat(v)
                    except (ValueError, TypeError):
                        ok = False
                        break
            if ok:
                items.append(it)
            else:
                bad += 1
        self._items = items
        if bad:
            self._corrupt = True
            print(f"[schedule.override_store] {bad} 条坏条目已剔除(字段非法),读入 {len(items)} 条: "
                  f"{self._path}", file=sys.stderr)

    def _save(self):
        data = json.dumps({"override_version": OVERRIDE_VERSION, "items": self._items},
                          ensure_ascii=False)
        tmp = Path(str(self._path) + ".tmp")
        tmp.write_text(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # ── 读 ──

    def items(self) -> list[dict]:
        return [dict(i) for i in self._items]

    def for_date(self, d: date) -> list[dict]:
        d_s = d.isoformat()
        out = []
        for i in self._items:
            if i["date"] == d_s:
                out.append(i)
            elif i.get("to_date") == d_s:
                out.append(i)  # R2:跨天 move 目标日呈现(day_plan/today_exceptions 同源)
            elif i.get("end_date") and i["date"] <= d_s <= i["end_date"]:
                out.append(i)
        return out

    def intervals(self) -> list[dict]:
        """区间事实:仅 exam_week(§3.2 ref 资格 = 按 kind 判定)。"""
        return [dict(i) for i in self._items if i["kind"] == "exam_week"]

    def reminders_in(self, start: date, end: date) -> list[dict]:
        s, e = start.isoformat(), end.isoformat()
        return [dict(i) for i in self._items if i["kind"] == "reminder" and s <= i["date"] <= e]

    def by_id(self, id_: str) -> dict | None:
        for i in self._items:
            if i["id"] == id_:
                return dict(i)
        return None

    # ── 校验 ──

    def validate(self, item: dict) -> None:
        """互斥矩阵 + 值级校验(§3.2)。协议层字段已在 api 剥离。"""
        kind = item.get("kind")
        if kind not in KINDS:
            raise OverrideError(f"未知 kind: {kind!r}")
        for key in ("date", "end_date", "to_date"):
            if item.get(key) is not None:
                try:
                    date.fromisoformat(item[key])
                except (ValueError, TypeError):
                    raise OverrideError(f"{key} 非法 ISO 日期: {item[key]!r}")
        if "period" in item and item.get("period") is not None:
            if not isinstance(item["period"], int) or not 1 <= item["period"] <= 11:
                raise OverrideError(f"period 越界: {item.get('period')!r}(须 1-11)")
        if "to_period" in item and item.get("to_period") is not None:
            if not isinstance(item["to_period"], int) or not 1 <= item["to_period"] <= 11:
                raise OverrideError(f"to_period 越界: {item.get('to_period')!r}(须 1-11)")
        for field in ("label", "note"):
            if item.get(field) is not None and len(str(item[field]).encode()) > MAX_FIELD_LEN:
                raise OverrideError(f"{field} 超长(>{MAX_FIELD_LEN} 字节)")
        if item.get("course"):
            if len(str(item["course"].get("course", "")).encode()) > MAX_FIELD_LEN:
                raise OverrideError("course 名超长(>100 字节)")
        if len(json.dumps(item, ensure_ascii=False).encode()) > MAX_ITEM_BYTES:
            raise OverrideError("item JSON 总长 > 4KB")
        # ── 互斥矩阵(§3.2)──
        has_end = item.get("end_date") is not None
        has_course = item.get("course") is not None
        has_label = item.get("label") is not None
        has_to = item.get("to_date") is not None or item.get("to_period") is not None
        if kind == "cancel":
            if item.get("period") is None:
                raise OverrideError("cancel 必有 period")
            if has_course or has_to or has_label:
                raise OverrideError("cancel 仅可带 date/end_date/period/note")
        elif kind == "move":
            # 源 period 形态要求与源槽无课检查 → Task 6(api 层持 kind 补全)
            if item.get("to_period") is None or has_end or has_label:
                raise OverrideError("move 必有 to_period,可带 to_date,无 end_date/label")
        elif kind == "add":
            if item.get("period") is None:
                raise OverrideError("add 必有 period")
            if not has_course or has_to or has_label:
                raise OverrideError("add 必有 course,无 to_*/label")
        elif kind == "exam_week":
            if item.get("period") is not None or has_to or not has_label:
                raise OverrideError("exam_week 无 period/to_*,必有 label")
        elif kind == "reminder":
            if has_course or has_to or not has_label:
                raise OverrideError("reminder 无 course/to_*,必有 label")

    # ── 写(经 api 调用)──

    def add(self, item: dict, now: datetime) -> tuple[dict, bool]:
        """追加或幂等替换(reminder date+label / exam_week date+end_date+label 全等)。
        返回 (落盘条目, 是否替换)。"""
        self.validate(item)
        created = now.astimezone(CST).strftime("%Y-%m-%dT%H:%M:%S%z")
        entry = {k: v for k, v in item.items() if v is not None}
        entry["created_at"] = created
        if "id" not in entry:
            entry["id"] = self._next_id(entry["date"])
        if self._idempotent_match(entry):
            for i, old in enumerate(self._items):
                if self._idempotent_match(old, entry):
                    entry["id"] = old["id"]  # 幂等替换保留原 id(引用稳定性)
                    self._items[i] = entry
                    self._save()
                    return dict(entry), True
        self._items.append(entry)
        self._save()
        return dict(entry), False

    def _next_id(self, date_s: str) -> str:
        prefix = f"ovr-{date_s.replace('-', '')}-"
        max_n = 0
        for i in self._items:
            if i["id"].startswith(prefix):
                try:
                    max_n = max(max_n, int(i["id"].rsplit("-", 1)[1]))
                except (IndexError, ValueError):
                    pass
        return f"{prefix}{max_n + 1}"

    @staticmethod
    def _idempotent_match(a: dict, b: dict | None = None) -> bool:
        if b is None:
            return (a["kind"] in ("reminder", "exam_week"))
        if a["kind"] != b["kind"]:
            return False
        if a["kind"] == "reminder":
            return a["date"] == b["date"] and a.get("label") == b.get("label")
        if a["kind"] == "exam_week":
            return (a["date"] == b["date"] and a.get("end_date") == b.get("end_date")
                    and a.get("label") == b.get("label"))
        return False

    def remove_by_id(self, id_: str) -> bool:
        before = len(self._items)
        self._items = [i for i in self._items if i["id"] != id_]
        if len(self._items) < before:
            self._save()
            return True
        return False

    def remove_exact(self, cond: dict, today: date) -> bool:
        """三选一 {id} | {date, period} | {date, label}。date+period 命中区间条目起始日 → 删整条(H3)。"""
        if "id" in cond:
            return self.remove_by_id(cond["id"])
        before = len(self._items)
        if "period" in cond:
            self._items = [i for i in self._items
                           if not (i["date"] == cond["date"] and i.get("period") == cond["period"])]
        elif "label" in cond:
            self._items = [i for i in self._items
                           if not (i["date"] == cond["date"] and i.get("label") == cond["label"])]
        else:
            raise OverrideError("remove match 必须为 {id} | {date,period} | {date,label}")
        if len(self._items) < before:
            self._save()
            return True
        return False

    def cleanup(self, today: date) -> int:
        """过期自动清理(§3.2 F1):分端点规则。执行者 = api 写后幂等调用。"""
        before = len(self._items)
        kept = []
        for i in self._items:
            d = date.fromisoformat(i["date"])
            if i["kind"] == "move":
                td = date.fromisoformat(i["to_date"]) if i.get("to_date") else d
                if max(d, td) < today:
                    continue
            elif i.get("end_date"):
                if date.fromisoformat(i["end_date"]) < today:
                    continue
            elif d < today:
                continue
            kept.append(i)
        removed = before - len(kept)
        if removed:
            self._items = kept
            self._save()
        return removed
