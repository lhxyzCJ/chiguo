# ============================================================
# anniversary_manager.py — 纪念日/倒计时 CRUD 管理
# 存储：anniversaries.json
# 两种类型：anniversary（每年重复，MM-DD）和 countdown（一次性，YYYY-MM-DD）
# ============================================================

import json
import os
import uuid
from dataclasses import dataclass, asdict, field
from datetime import date
from pathlib import Path

@dataclass
class Anniversary:
    id: str
    type: str          # "anniversary" | "countdown"
    name: str          # 人类可读名称
    date: str          # "MM-DD" (anniversary) 或 "YYYY-MM-DD" (countdown)
    note: str = ""
    created_at: str = ""


class AnniversaryManager:
    """纪念日/倒计时 CRUD 管理。持久化到 JSON 文件。"""

    def __init__(self, data_path: str = "anniversaries.json"):
        self._path = self._resolve_data_path(data_path)
        self._items: list[Anniversary] = []
        self._load()

    @staticmethod
    def _resolve_data_path(data_path: str) -> Path:
        """解析存储路径：
        - 绝对路径 → 原样使用（外部显式传入仍生效）
        - 相对路径且 cwd 已存在同名文件 → 用 cwd 文件（兼容旧版/测试隔离目录）
        - 相对路径且 cwd 无此文件 → 锚定模块目录（防止从其他 cwd 运行把数据写散）"""
        p = Path(data_path)
        if p.is_absolute():
            return p
        cwd_candidate = Path.cwd() / data_path
        if cwd_candidate.exists():
            return cwd_candidate
        return Path(__file__).resolve().parent / p

    # ── 持久化 ──────────────────────────────────────────

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text())
                anns = data.get("anniversaries", [])
                self._items = [Anniversary(**a) for a in anns if self._valid(a)]
            except (json.JSONDecodeError, TypeError):
                self._items = []

    def _save(self):
        data = json.dumps({
            "anniversaries": [asdict(a) for a in self._items],
        }, indent=2, ensure_ascii=False)
        tmp = Path(str(self._path) + ".tmp")
        tmp.write_text(data)
        os.replace(tmp, self._path)

    @staticmethod
    def _valid(a: dict) -> bool:
        """基础字段校验"""
        return all(k in a for k in ("id", "type", "name", "date"))

    # ── CRUD ────────────────────────────────────────────

    def add(self, type_: str, name: str, date_str: str, note: str = "") -> Anniversary:
        """
        添加纪念日或倒计时。
        Raises ValueError on invalid format.
        """
        if type_ not in ("anniversary", "countdown"):
            raise ValueError(f"type must be 'anniversary' or 'countdown', got '{type_}'")

        # 校验日期格式
        if type_ == "anniversary":
            date.fromisoformat(f"2024-{date_str}")  # 用闰年测试 MM-DD 合法性
        else:
            date.fromisoformat(date_str)  # 测试 YYYY-MM-DD 合法性

        a = Anniversary(
            id=uuid.uuid4().hex[:12],
            type=type_,
            name=name.strip(),
            date=date_str.strip(),
            note=note.strip(),
            created_at=date.today().isoformat(),
        )
        self._items.append(a)
        self._save()
        return a

    def remove(self, id_: str) -> bool:
        """按 id 删除。返回是否找到并删除。"""
        before = len(self._items)
        self._items = [a for a in self._items if a.id != id_]
        if len(self._items) < before:
            self._save()
            return True
        return False

    def list_all(self) -> list[Anniversary]:
        """返回所有纪念日副本（按类型+日期排序）。"""
        def _sort_key(a):
            return (0 if a.type == "countdown" else 1, a.date)
        return sorted(self._items, key=_sort_key)

    def update(self, id_: str, **kwargs) -> Anniversary | None:
        """
        更新纪念日。可更新字段：name, date, type, note。
        如果更新 date，会重新校验格式。
        Returns updated Anniversary or None if not found.
        """
        for a in self._items:
            if a.id == id_:
                if "name" in kwargs:
                    a.name = kwargs["name"].strip()
                if "note" in kwargs:
                    a.note = kwargs["note"].strip()
                if "type" in kwargs:
                    if kwargs["type"] not in ("anniversary", "countdown"):
                        raise ValueError(f"type must be 'anniversary' or 'countdown'")
                    a.type = kwargs["type"]
                if "date" in kwargs:
                    d = kwargs["date"].strip()
                    if a.type == "anniversary":
                        date.fromisoformat(f"2024-{d}")
                    else:
                        date.fromisoformat(d)
                    a.date = d
                self._save()
                return a
        return None

    # ── 查询 ────────────────────────────────────────────

    def get_today(self, today: date) -> list[Anniversary]:
        """
        返回今天匹配的所有纪念日。
        anniversary: 月和日匹配
        countdown: 完整日期匹配
        """
        mmdd = today.strftime("%m-%d")
        yyyymmdd = today.isoformat()
        result = []
        for a in self._items:
            if a.type == "anniversary" and a.date == mmdd:
                result.append(a)
            elif a.type == "countdown" and a.date == yyyymmdd:
                result.append(a)
        return result

    def get_upcoming(self, today: date, days: int = 7) -> list[tuple[Anniversary, int]]:
        """
        返回未来 days 天内的纪念日，按距离今天的天数升序排列。
        正确处理年边界：12月查1月纪念日 → 查下一年。
        """
        result = []
        today_ord = today.toordinal()

        for a in self._items:
            if a.type == "countdown":
                try:
                    d = date.fromisoformat(a.date)
                except ValueError:
                    continue
                delta = d.toordinal() - today_ord
                if 0 < delta <= days:
                    result.append((a, delta))

            elif a.type == "anniversary":
                # 今年
                try:
                    d_this = date.fromisoformat(f"{today.year}-{a.date}")
                except ValueError:
                    # 2月29日在非闰年 → 当2月28日
                    if a.date == "02-29":
                        d_this = date(today.year, 2, 28)
                    else:
                        continue
                delta = d_this.toordinal() - today_ord
                if 0 < delta <= days:
                    result.append((a, delta))
                elif delta <= 0:
                    # 今年已过，查明年
                    try:
                        d_next = date.fromisoformat(f"{today.year + 1}-{a.date}")
                    except ValueError:
                        if a.date == "02-29":
                            d_next = date(today.year + 1, 2, 28)
                        else:
                            continue
                    delta = d_next.toordinal() - today_ord
                    if 0 < delta <= days:
                        result.append((a, delta))

        result.sort(key=lambda x: x[1])
        return result

    def cleanup(self) -> int:
        """清理已过期的倒计时（countdown 类型且 date < today）。返回删除数。"""
        today = date.today()
        before = len(self._items)
        self._items = [
            a for a in self._items
            if not (a.type == "countdown" and date.fromisoformat(a.date) < today)
        ]
        removed = before - len(self._items)
        if removed > 0:
            self._save()
        return removed
