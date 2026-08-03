# ============================================================
# schedule/api.py — 全部时间安排的唯一写入口(§6,唯一写方)
# 写 = 校验 → 原子写 → 确认文案;迁移/物化守卫挂写类调用点惰性首执。
# 铁律:读路径(day_plan/T1/--attention/--schedule-recall/recall/引擎)永不进此模块的写路径。
# 本批(批次 2b)when 仅收显式 YYYY-MM-DD 与 MM-DD 两种令牌(含区间 end_date);
# 完整七形态换算/形态约束/过去日期分端点/源槽无课检查 → Task 6。
# ============================================================

import json
import os
import sys
from datetime import date, datetime

from schedule.override_store import OverrideStore, OverrideError, CST
from schedule.plan_store import PlanStore
from schedule import anniversary
from schedule.confirm import build_confirmation, build_question


class ApiRejection(Exception):
    """确定性拒绝。category 供 daemon 映射 H5 文案(H5_TEMPLATES)。"""

    def __init__(self, category: str, detail: str = ""):
        super().__init__(f"{category}: {detail}".rstrip(": "))
        self.category = category


class ScheduleApi:
    def __init__(self, base_dir: str, config: dict | None = None):
        self.base_dir = os.path.abspath(base_dir)  # 绝对锚定,防 cwd 依赖(批 2 遗留修正)
        self.config = config or {}
        self.overrides = OverrideStore(self.base_dir)
        self.anniversary_mgr = anniversary.AnniversaryManager(self.base_dir)
        self.plan_store = PlanStore(self.base_dir)
        self._migrated = False
        # 迁移子项激活开关(批次注记):② 激活批次 = 6c(Task 14);③④ 激活批次 = 4(Task 10)
        self._enable_countdown_migration = False
        self._enable_toml_migrations = False

    # ── 迁移/物化守卫(惰性首执,只读子命令永不触发,H1)──

    def _guard(self):
        if not self._migrated:
            self.ensure_migrations()
            self._migrated = True

    def ensure_migrations(self):
        """五子项全序(spec §3.1 M2/N3/F3,二十轮补 0):
        0. overrides 损坏重建 → ①. anniversaries 损坏重建 → ②. countdown→reminder(6c 激活)
        → ③. toml exam_weeks(批 4 激活) → ④. toml special_dates 合并(批 4 激活)。
        0 必须先于 ②:② 写 overrides,若重建在其后,刚迁入的 reminder 被空文件抹掉。"""
        # 0. overrides 损坏 → 重建为合法空文件(非 0 字节,二十轮 LOW 钉死)
        if self.overrides.corrupt:
            self.overrides._items = []  # noqa: 内部重建;写经 _save
            self.overrides._corrupt = False
            self.overrides._save()
            print(f"[schedule.api] overrides 损坏已重建为空集: {self.overrides.path}", file=sys.stderr)
        # ①. anniversaries 损坏 → 重建为默认生日(视同缺失路径,N1)
        if self.anniversary_mgr._corrupt or not self.anniversary_mgr._path.exists():
            self._materialize_anniversaries()
        # ②. countdown→reminder 防御迁移(激活 = 6c;幂等 label+date 去重,F4/N3)
        if self._enable_countdown_migration:
            self._migrate_countdown()
        # ③④. toml 一次性迁移(激活 = 批 4,见 Task 10)
        if self._enable_toml_migrations:
            self._migrate_toml_exam_weeks()
            self._migrate_toml_special_dates()

    def _materialize_anniversaries(self):
        """① 与 api 首写物化共用:当前内存合并视图(默认 + 用户条目)落盘。
        文件缺失/损坏 → 默认生日;迁移写入即物化(R1)。
        默认条目无 id(A16 决议)→ 合成 anniv-{date}(旧 anniversary_manager 为 uuid 风格,
        此处取确定性合成保证重建幂等)。"""
        from schedule.anniversary import DEFAULT_ANNIVERSARIES
        raw = self.anniversary_mgr.visible_items()
        if not raw:
            raw = list(DEFAULT_ANNIVERSARIES)
        items = []
        for a in raw:
            it = dict(a) if isinstance(a, dict) else {
                "id": a.id, "type": a.type, "name": a.name, "date": a.date,
                "note": a.note, "created_at": a.created_at}
            if "id" not in it:
                it = dict(it, id=f"anniv-{it['date']}")
            items.append(it)
        self.anniversary_mgr._items = [anniversary.Anniversary(**it) for it in items]
        self.anniversary_mgr._save()
        self.anniversary_mgr._corrupt = False

    def _migrate_countdown(self):
        """②:直读 anniversaries.json 原始文件(不经 _load 白名单,防数据丢失,M2)。"""
        p = self.anniversary_mgr._path
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return  # 损坏已由 ① 处理(解析失败落入 ① 重建,F3)
        items = raw.get("anniversaries", [])
        migrated = 0
        for it in items:
            if it.get("type") == "countdown":
                try:
                    date.fromisoformat(it["date"])
                except (KeyError, ValueError):
                    continue
                self.apply_override({"kind": "reminder", "date": it["date"],
                                     "label": it.get("name", ""), "note": "from countdown"},
                                    _from_migration=True)
                migrated += 1
        if migrated:
            kept = [it for it in items if it.get("type") != "countdown"]
            p.write_text(json.dumps({"anniversaries": kept}, ensure_ascii=False, indent=2))
            self.anniversary_mgr._load()

    def _migrate_toml_exam_weeks(self):
        """③(Task 10 激活):toml exam_weeks → override,label="from toml 考试周"。"""
        sched = self.config.get("schedule", {})
        for r in sched.get("exam_weeks", []) or []:
            parts = [x.strip() for x in str(r).split(",")]
            if len(parts) != 2:
                continue
            try:
                s, e = date.fromisoformat(parts[0]), date.fromisoformat(parts[1])
            except ValueError:
                continue
            self.apply_override({"kind": "exam_week", "date": s.isoformat(),
                                 "end_date": e.isoformat(), "label": "from toml 考试周"},
                                _from_migration=True)

    def _migrate_toml_special_dates(self):
        """④(Task 10 激活):toml special_dates → 纪念日(迟菓生日为默认,其余 name="特殊日期 MM-DD")。"""
        sched = self.config.get("schedule", {})
        for mmdd in sched.get("special_dates", []) or []:
            if mmdd == "05-11":
                continue  # 默认生日(代码内置,物化时已含)
            if any(a.get("date") == mmdd and a.get("note") == "from toml"
                   for a in self.anniversary_mgr.visible_items()):
                continue  # 幂等:④ 重复执行不重复合并
            self.anniversary_mgr.add("anniversary", f"特殊日期 {mmdd}", mmdd, note="from toml")

    # ── when 换算(本批:显式 YYYY-MM-DD / MM-DD 两种;完整七形态 → Task 6 换用 resolve_when 模块)──

    def _resolve_date(self, when: dict, today: date) -> date:
        if not isinstance(when, dict):
            raise ApiRejection("ambiguous", f"when 形态非法: {when!r}(完整换算落批次 2c)")
        v = when.get("date")
        try:
            return date.fromisoformat(v)
        except (TypeError, ValueError):
            pass
        if isinstance(v, str) and len(v) == 5 and v[2] == "-":  # MM-DD(两位月两位日,前导零)
            try:
                d = anniversary.mmdd_to_date(v, today.year)      # 02-29 非闰年兜底 02-28
                nxt = anniversary.mmdd_to_date(v, today.year + 1)
            except ValueError:
                raise ApiRejection("invalid_value", f"MM-DD 非法: {v}")
            if not (1 <= int(v[:2]) <= 12 and 1 <= int(v[3:]) <= 31):
                raise ApiRejection("invalid_value", f"MM-DD 非法: {v}")
            return d if d >= today else nxt  # inferYear:今年该日期已过 → 明年(当天留今年)
        raise ApiRejection("invalid_value", f"date 非法: {v!r}")

    # ── 写接口(§6)──

    def apply_override(self, item: dict, _from_migration: bool = False) -> dict:
        """协议形态 item → when 换算(本批两令牌)→ 校验 → 原子写 → 确认文案。
        kind=remove 显式拒绝(路由 remove_override);when 泄漏其余令牌 → 拒绝。"""
        if not isinstance(item, dict):
            raise ApiRejection("invalid_value", "item 非 dict")
        kind = item.get("kind")
        if kind == "remove":
            raise ApiRejection("invalid_value", "apply_override 拒绝 kind=remove(路由 remove_override)")
        if kind not in ("cancel", "move", "add", "exam_week", "reminder"):
            raise ApiRejection("invalid_value", f"未知 kind: {kind!r}")
        when = item.get("when")
        if when is not None:
            if not isinstance(when, dict) or set(when) - {"date", "end_date"}:
                raise ApiRejection("ambiguous", f"when 令牌本批仅收 date/end_date(完整换算落 Task 6): {when!r}")
            if "date" not in when:
                raise ApiRejection("ambiguous", f"when 缺 date(完整换算落 Task 6): {when!r}")
            entry = {k: v for k, v in item.items() if k != "when"}
            entry["date"] = self._resolve_date(when, date.today()).isoformat()
            if "end_date" in when:
                entry["end_date"] = when["end_date"]
        else:
            entry = dict(item)
            if not isinstance(entry.get("date"), str):
                raise ApiRejection("ambiguous", "when/date 缺失(协议层 must 带 when)")
        if "to_date" in entry and isinstance(entry["to_date"], dict):
            td = entry["to_date"]
            if set(td) != {"date"}:
                raise ApiRejection("invalid_value", f"to_date 令牌非法: {td!r}")
            entry["to_date"] = td["date"]
        allowed = {"kind", "date", "end_date", "to_date", "to_period", "period", "course", "label", "note"}
        unknown = set(entry) - allowed
        if unknown:
            raise ApiRejection("invalid_value", f"未知字段: {sorted(unknown)}")
        if not _from_migration:
            self._guard()  # 惰性迁移(只读路径永不触发)
        if "to_date" in entry and kind != "move":
            raise ApiRejection("shape_mismatch", "to_date 仅 move 可用(形态违规)")
        if kind == "move":
            if entry.get("to_date") is not None and not isinstance(entry["to_date"], str):
                raise ApiRejection("invalid_value", "to_date 必须为 ISO 字符串")
            if entry.get("to_period") is not None and entry.get("to_date") is not None and \
                    entry["to_period"] == entry.get("period") and entry["to_date"] == entry.get("date"):
                raise ApiRejection("shape_mismatch", "to_date==date 且 to_period==period(无变化)")
        try:
            entry, replaced = self.overrides.add(entry, datetime.now(CST))
        except OverrideError as e:
            raise ApiRejection("invalid_value", str(e))
        self.overrides.cleanup(date.today())  # 写后幂等清理(F1 执行者 = api)
        return {"ok": True, "action": "schedule_change", "replaced": replaced,
                "item": entry, "text": build_confirmation(entry)}

    def remove_override(self, match: dict) -> dict:
        """match 三选一 {id} | {date,period} | {date,label}(LOW 钉死不组合);
        match.date 为 when 令牌(单日形态,复用 _resolve_date 换算,F7——"取消周三停课"的"周三"
        为 weekday 令牌时引擎换算后精确匹配;区间形态 → 拒绝追问)。"""
        self._guard()
        if not isinstance(match, dict) or not match:
            raise ApiRejection("invalid_value", "match 非 dict 或为空")
        if set(match) - {"id", "date", "period", "label"}:
            raise ApiRejection("shape_mismatch", "match 必须为 {id} | {date,period} | {date,label} 之一")
        if "id" in match:
            if len(match) != 1:
                raise ApiRejection("shape_mismatch", "match 三选一,不组合")
            cond = {"id": match["id"]}
        else:
            if "date" not in match or not (("period" in match) ^ ("label" in match)):
                raise ApiRejection("shape_mismatch", "match 必须为 {date,period} | {date,label}")
            date_token = match["date"]
            if isinstance(date_token, dict):
                if set(date_token) - {"date", "days", "weekday", "week_offset"}:
                    raise ApiRejection("shape_mismatch", "match 日期只收单日形态(区间形态拒绝,F7)")
                cond = {"date": self._resolve_date(date_token, date.today()).isoformat()}
            elif isinstance(date_token, str):
                cond = {"date": date_token}
            else:
                raise ApiRejection("invalid_value", "match.date 非法")
            cond.update({"period": match["period"]} if "period" in match else {"label": match["label"]})
        ok = self.overrides.remove_exact(cond, date.today())
        if not ok:
            raise ApiRejection("not_found", f"零匹配: {match!r}")
        return {"ok": True, "action": "schedule_change", "removed": cond,
                "text": "好,那条安排取消掉了,记下了。"}

    # ── 纪念日/寒暑假(批 5 daemon 改调,本批给直通)──

    def add_anniversary(self, type_, name, date_str, note=""):
        self._guard()
        a = self.anniversary_mgr.add(type_, name, date_str, note=note)
        return {"action": "anniversary_added", "ok": True, "id": a.id, "name": a.name,
                "date": a.date, "type": a.type}

    def remove_anniversary(self, id_):
        self._guard()
        return {"action": "anniversary_removed", "ok": self.anniversary_mgr.remove(id_)}

    def list_anniversaries(self):
        return {"action": "anniversary_list", "anniversaries": [
            {"id": a.id, "type": a.type, "name": a.name, "date": a.date,
             "note": a.note, "created_at": a.created_at}
            for a in self.anniversary_mgr.list_all()], "count": len(self.anniversary_mgr.list_all())}

    def update_anniversary(self, id_, **kwargs):
        self._guard()
        a = self.anniversary_mgr.update(id_, **kwargs)
        return {"action": "anniversary_updated", "ok": a is not None,
                "anniversary": {"id": a.id, "type": a.type, "name": a.name, "date": a.date} if a else None}

    # set_break 落 Task 11(批 5):daemon --break 处理器整体迁入(行为保持现 --break 语义,
    # 写路径归 api 唯一写口,§3.1/§6)
