# ============================================================
# schedule/api.py — 全部时间安排的唯一写入口(§6,唯一写方)
# 写 = 校验 → 原子写 → 确认文案;迁移/物化守卫挂写类调用点惰性首执。
# 铁律:读路径(day_plan/T1/--attention/--schedule-recall/recall/引擎)永不进此模块的写路径。
# Task 6(批次 2c):when 全七形态经 resolve_when 纯换算;形态约束 → 学期边界 → 分端点过去校验
# → to_date 五态 → move 源槽/快照;仍兼容批 2b 旧协议显式 date/end_date 字段。
# ============================================================

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from schedule.override_store import OverrideStore, OverrideError
from schedule.plan_store import PlanStore
from schedule import anniversary
from schedule.confirm import build_confirmation, build_question
from schedule.resolve_when import resolve_when, ResolveReject
from schedule.day_plan import week_number
from schedule.migrations import ensure_migrations
from chiguo_time import CST  # Q22: 共享时区常量
from chiguo_atomic import atomic_write  # Q23: 共享原子写助手


class ApiRejection(Exception):
    """确定性拒绝。category 供 daemon 映射 H5 文案(H5_TEMPLATES)。"""

    def __init__(self, category: str, detail: str = ""):
        super().__init__(f"{category}: {detail}".rstrip(": "))
        self.category = category


class ScheduleApi:
    def __init__(self, base_dir: str, config: dict | None = None, today: date | None = None):
        self.base_dir = os.path.abspath(base_dir)  # 绝对锚定,防 cwd 依赖(批 2 遗留修正)
        self.config = config or {}
        self.today = today or date.today()
        self.overrides = OverrideStore(self.base_dir)
        self.anniversary_mgr = anniversary.AnniversaryManager(self.base_dir)
        self.plan_store = PlanStore(self.base_dir)
        self._migrated = False
        # 迁移已全部激活并跑完（②=6c Task14；③④=批 4 Task10），调用内联（历史开关结构已清理）

    # ── 迁移/物化守卫(惰性首执,只读子命令永不触发,H1)──

    def _guard(self):
        if not self._migrated:
            ensure_migrations(self)
            self._migrated = True

    # ── when 换算(完整七形态;形态约束先于学期边界,保证单日 kind 收区间 → shape_mismatch 与学期状态无关)──

    def _semester_dates(self) -> tuple[date, date | None]:
        sched = self.config.get("schedule", {})
        raw = sched.get("semester_start", "")
        try:
            ss = date.fromisoformat(str(raw))
        except ValueError:
            ss = date(2026, 2, 23)
            print(f"[warn] [schedule].semester_start 缺失/非法（{str(raw)!r}），回退默认 {ss}；"
                  f"课表周次/学期边界将基于回退值计算，请更新 chiguo_proactive.toml",
                  file=sys.stderr)
        se = None
        try:
            if sched.get("semester_end"):
                se = date.fromisoformat(str(sched["semester_end"]))
        except ValueError:
            pass
        return ss, se

    # ── apply_override 流水(#377):validate → normalize → check → materialize → persist ──
    # to_date 拒绝形态:week_offset 单键 / start-end 双键 / 空 dict 一律 shape_mismatch。
    _TO_DATE_REJECTED = ({"week_offset"}, {"start", "end"}, set())

    def _validate_kind(self, item: dict) -> tuple[str, dict]:
        """校验 kind/未知字段 → (kind, entry 基底)。"""
        kind = item.get("kind")
        if kind == "remove":
            raise ApiRejection("invalid_value", "apply_override 拒绝 kind=remove(路由 remove_override)")
        if kind not in ("cancel", "move", "add", "exam_week", "reminder"):
            raise ApiRejection("invalid_value", f"未知 kind: {kind!r}")
        unknown = set(item) - {"kind", "when", "date", "end_date", "period", "to_period",
                               "to_date", "course", "label", "note"}
        if unknown:
            raise ApiRejection("invalid_value", f"未知字段: {sorted(unknown)}")
        return kind, {k: v for k, v in item.items() if k not in ("when",) and v is not None}

    @staticmethod
    def _normalize_legacy_when(item: dict):
        """旧协议归一(批 2b deprecation):when 缺失 → 顶层 date 归一进 when 管线;缺 date → 歧义拒绝。"""
        when = item.get("when")
        if when is None:
            if "date" not in item:
                raise ApiRejection("ambiguous", "when/date 缺失 → 歧义拒绝")
            when = {"date": item["date"]}   # 顶层 date 形态 → 归一(start/end/过去校验全走)
        if not isinstance(when, dict):
            raise ApiRejection("invalid_value", "when 非 dict")
        return when

    def _resolve_entry_when(self, kind: str, when: dict, entry: dict,
                            today: date, semester_start, semester_end,
                            check_past: bool = True) -> date:
        """when 全令牌换算 → 形态约束 → 学期边界 → 字段落点 → 区间不变量 → 分端点过去校验。
        直接在 entry 上落 date/end_date；返回 start。check_past=False = 迁移写豁免。"""
        try:
            if set(when) == {"date", "end_date"}:
                start, _ = resolve_when({"date": when["date"]}, today, semester_start)
                end, _ = resolve_when({"date": when["end_date"]}, today, semester_start)
                if (end - start).days > 60:
                    # A20-05 (R11): {date,end_date} 批 2b 路径并入统一跨度检查(与 {start,end} 一致),
                    # 修复前 78 天区间绕过落盘。
                    raise ApiRejection("invalid_value", "跨度 > 60 天")
                entry["end_date"] = end.isoformat()   # 归一(MM-DD → ISO),修 A20-05 格式不一致拒绝
                is_interval = True
            else:
                start, end = resolve_when(when, today, semester_start)
                is_interval = ("week_offset" in when and "weekday" not in when) \
                    or set(when) == {"start", "end"}
        except ResolveReject as e:
            raise ApiRejection(e.category, str(e))
        # ── 形态约束(§4.2 C3/F1/F-C)──
        if kind in ("reminder", "move") and is_interval:
            raise ApiRejection("shape_mismatch", f"{kind} 不收区间形态(单日 kind)")
        # ── 学期边界(二十轮对称化):week_offset 非 cancel 类 → 学期前/目标周超学期周数拒绝 ──
        if "week_offset" in when and kind in ("reminder", "add", "exam_week", "move"):
            if today < semester_start:
                raise ApiRejection("before_semester", "学期前 week_offset 语义失效")
            if semester_end and week_number(start, semester_start) > \
                    week_number(semester_end, semester_start):
                raise ApiRejection("after_semester", "目标周超出学期周数")
        # ── 字段落点(§4.2 F-B):(start,end) 落 date/end_date,不落 to_date ──
        if kind in ("cancel", "add"):
            entry["date"] = start.isoformat()
            if is_interval and entry.get("end_date") is None:
                entry["end_date"] = end.isoformat()
        elif kind == "exam_week":
            entry["date"] = start.isoformat()
            if entry.get("end_date") is None:
                entry["end_date"] = end.isoformat()   # 单日 → 退化 date=end_date(F-D)
        else:  # reminder / move
            entry["date"] = start.isoformat()
        # ── 区间顺序不变量(R11):entry 同含 date/end_date → end_date 不得早于 date ──
        # 统一兜底 when={"date","end_date"} 与顶层 end_date 双路径,防死 override 落盘;
        # 经 resolve_when 解析(兼容 ISO/MM-DD 双格式,与 date 解析一致)
        if entry.get("end_date") is not None:
            try:
                _d1, _ = resolve_when({"date": entry["end_date"]}, today, semester_start)
            except ResolveReject as e:
                raise ApiRejection(e.category, str(e))
            if _d1 < start:
                raise ApiRejection("invalid_value", "区间 end_date 早于 date,死区间拒绝")
            if (_d1 - start).days > 60:
                raise ApiRejection("invalid_value", "跨度 > 60 天")   # A20-05:顶层 end_date 路径统一跨度检查
            entry["end_date"] = _d1.isoformat()   # A20-05:归一顶层 end_date(MM-DD → ISO),不再格式不一致拒绝
        # ── 过去日期分端点校验(L2/C3/F1):课程例外与区间事实查 end;单日查 date ──
        if check_past and kind in ("cancel", "add", "exam_week"):
            try:
                check = date.fromisoformat(entry.get("end_date") or end.isoformat())
            except ValueError:
                raise ApiRejection("invalid_value", f"end_date 非法: {entry.get('end_date')!r}")
            if check < today:
                raise ApiRejection("past_date", f"结果 {check} < today")
        elif check_past and kind == "reminder":
            if start < today:
                raise ApiRejection("past_date", f"结果 {start} < today")
        elif check_past and kind == "move" and not entry.get("to_date"):
            # move 无 to_date = 单日条目 → 查 date;有 to_date 由下方 to_date 检查约束
            if start < today:
                raise ApiRejection("past_date", f"结果 {start} < today")
        return start

    def _resolve_to_date(self, kind: str, item: dict, entry: dict,
                         today: date, semester_start) -> None:
        """to_date(move 独立字段;五态单日形态,不收 week_offset 单/start-end,C2/M4)。直接落 entry。"""
        if item.get("to_date") is None:
            return
        if kind != "move":
            raise ApiRejection("shape_mismatch", "to_date 仅 move 可用")
        td = item["to_date"]
        if isinstance(td, dict):
            if set(td) in self._TO_DATE_REJECTED:
                raise ApiRejection("shape_mismatch", "to_date 不收 week_offset 单/start-end")
            try:
                ts, te = resolve_when(td, today, semester_start)
            except ResolveReject as e:
                raise ApiRejection(e.category, str(e))
            if ts != te:
                raise ApiRejection("shape_mismatch", "to_date 必须为单日")
        else:
            try:
                ts = te = date.fromisoformat(str(td))
            except ValueError:
                raise ApiRejection("invalid_value", f"to_date 非法: {td!r}")
        if ts < today:
            raise ApiRejection("past_date", "to_date 已过去(查 date 语义)")
        if ts < date.fromisoformat(entry["date"]):
            raise ApiRejection("shape_mismatch", "to_date < date(倒序调课)")
        entry["to_date"] = ts.isoformat()

    def _materialize_move(self, kind: str, entry: dict, item: dict,
                          semester_start) -> None:
        """move 源槽检查 + 课程快照 weeks 派生(M7)。直接改 entry。"""
        if kind == "move":
            if (entry.get("to_date") == entry.get("date")
                    and item.get("to_period") == item.get("period")):
                raise ApiRejection("shape_mismatch", "to_date==date 且 to_period==period(无变化)")
            if entry.get("period") is None:
                # A20-06 (R11): Task 6 注释承诺的 api 层补全——源 period 必填(validate 同款兜底)。
                # 修复前:无 period move 跳过源槽检查落盘 → 源槽不清 + 目标槽空课条目。
                raise ApiRejection("invalid_value", "move 必有源 period")
            src_course = self._move_source_course(entry)   # 参照系 = 基底课表 + 已应用 add 例外
            if src_course is None:
                raise ApiRejection("no_source_class", "move 源槽无课")
            if not entry.get("course"):
                entry["course"] = {k: v for k, v in src_course.items()
                                   if k in ("course", "teacher", "weeks", "weeks_raw",
                                            "location", "alternates")}
        # ── move/add 课程快照 weeks 派生(M7):weeks=[当日周次]/weeks_raw/alternates=[] ──
        if kind in ("move", "add") and entry.get("course"):
            c = dict(entry["course"])
            wk = week_number(date.fromisoformat(entry["date"]), semester_start)
            if isinstance(c.get("weeks"), set):
                c["weeks"] = sorted(c["weeks"])   # 内存规范形(set)→ 存储形(list)
            c.setdefault("weeks", [wk])
            c.setdefault("weeks_raw", f"第{wk}周")
            alts = []
            for a in (c.get("alternates") or []):
                a = dict(a)
                if isinstance(a.get("weeks"), set):
                    a["weeks"] = sorted(a["weeks"])
                alts.append(a)
            c["alternates"] = alts
            entry["course"] = c

    def apply_override(self, item: dict, _from_migration: bool = False) -> dict:
        """协议形态 item → resolve_when 全令牌换算 → 形态约束 → 学期边界 → 分端点过去校验
        → to_date 五态 → move 源槽/快照 → 幂等写 → 写后清理 → 确认文案。
        兼容批 2b 旧协议显式 date/end_date(顶层或 when 两键),Task 6 七形态全收。
        本体仅编排:validate → normalize → check → materialize → persist。"""
        if not isinstance(item, dict):
            raise ApiRejection("invalid_value", "item 非 dict")
        kind, entry = self._validate_kind(item)
        if not _from_migration:
            self._guard()
        today = self.today
        semester_start, semester_end = self._semester_dates()
        # 迁移写豁免过去校验:一次性迁移含历史条目(如已结束学期的考试周),
        # 校验/清理在迁移后的常规写调用点照常执行
        when = self._normalize_legacy_when(item)
        self._resolve_entry_when(kind, when, entry, today, semester_start,
                                 semester_end, check_past=not _from_migration)
        self._resolve_to_date(kind, item, entry, today, semester_start)
        self._materialize_move(kind, entry, item, semester_start)
        try:
            e2, replaced = self.overrides.add(entry, datetime.now(CST))
        except OverrideError as ex:
            raise ApiRejection("invalid_value", str(ex))
        if not _from_migration:
            self.overrides.cleanup(today)  # 迁移写豁免清理:一次性迁移含历史条目(常规写仍清理)
        return {"ok": True, "action": "schedule_change", "replaced": replaced,
                "item": e2, "text": build_confirmation(e2)}

    def _move_source_course(self, entry: dict) -> dict | None:
        """move 源槽课程:基底课表(week_courses)或已应用 add 例外;None = 源槽无课。"""
        from schedule.sources import load_sources
        from schedule.day_plan import week_courses
        src = load_sources(self.base_dir, self.config)
        d = date.fromisoformat(entry["date"])
        w = week_number(d, src.semester_start)
        base = week_courses(src.schedule, src.semester_start, w).get(d.weekday(), {})
        if entry["period"] in base:
            return base[entry["period"]]
        for ov in src.overrides.for_date(d):
            if ov["kind"] == "add" and ov.get("period") == entry["period"]:
                return ov.get("course")
        return None

    def remove_override(self, match: dict) -> dict:
        """match 三选一 {id} | {date,period} | {date,label},不组合(LOW);
        match.date 为 when 令牌(单日形态,复用 resolve_when,F7);区间形态 → shape_mismatch。"""
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
            dt = match["date"]
            if isinstance(dt, dict):
                if not dt or set(dt) == {"week_offset"} or set(dt) == {"start", "end"} \
                        or set(dt) - {"date", "days", "weekday", "week_offset"}:
                    raise ApiRejection("shape_mismatch", "match 日期只收单日形态(F7:区间形态拒绝)")
                try:
                    s, e = resolve_when(dt, self.today, self._semester_dates()[0])
                except ResolveReject as ex:
                    raise ApiRejection(ex.category, str(ex))
                if s != e:
                    raise ApiRejection("shape_mismatch", "match 日期必须为单日")
                cond = {"date": s.isoformat()}
            elif isinstance(dt, str):
                cond = {"date": dt}
            else:
                raise ApiRejection("invalid_value", "match.date 非法")
            cond.update({"period": match["period"]} if "period" in match else {"label": match["label"]})
        ok = self.overrides.remove_exact(cond, self.today)
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
        """列表合并读两本子(6c):anniversary 类型 + overrides reminder(统一展示)。"""
        items = [{"id": a.id, "type": a.type, "name": a.name, "date": a.date,
                  "note": a.note, "created_at": a.created_at}
                 for a in self.anniversary_mgr.list_all()]
        for r in self.overrides.items():
            if r["kind"] == "reminder":
                items.append({"id": r["id"], "type": "reminder", "name": r["label"],
                              "date": r["date"], "note": "", "created_at": r.get("created_at", "")})
        items.sort(key=lambda x: (x["date"], x["name"]))
        return {"action": "anniversary_list", "anniversaries": items, "count": len(items)}

    def update_anniversary(self, id_, **kwargs):
        self._guard()
        a = self.anniversary_mgr.update(id_, **kwargs)
        return {"action": "anniversary_updated", "ok": a is not None,
                "anniversary": {"id": a.id, "type": a.type, "name": a.name, "date": a.date} if a else None}

    # ── 寒暑假写路径(批 5 自 daemon --break 迁入,行为保持:输出形状逐键一致)──

    def set_break(self, cmd_line: str) -> dict:
        """on/off/add/remove/list/clear/status。break_state.json 锚定 base_dir;
        写 = tmp+os.replace+fsync(沿现先例);on_break/status 判定沿用现语义(真实今天)。"""
        bp = Path(self.base_dir) / "break_state.json"
        parts = cmd_line.split()
        cmd = parts[0] if parts else ""

        def _load():
            if bp.exists():
                try:
                    return json.loads(bp.read_text())
                except (ValueError, TypeError, OSError):
                    pass
            return {"breaks": []}

        def _save(data):
            atomic_write(bp, json.dumps(data, ensure_ascii=False, indent=2),
                         fsync=True)

        if cmd == "on":
            self._guard()
            data = _load()
            data["manual_override"] = True
            data["since"] = datetime.now(CST).isoformat()
            _save(data)
            return {"action": "break_set", "manual_override": True,
                    "message": "假期模式已开启（无限期），availability 恒为 0.85"}
        if cmd == "off":
            self._guard()
            if bp.exists():
                bp.unlink()
            return {"action": "break_set", "manual_override": False,
                    "message": "假期模式已关闭，所有区间已清空"}
        if cmd == "add" and len(parts) >= 3:
            start_str, end_str = parts[1], parts[2]
            note = " ".join(parts[3:]) if len(parts) > 3 else ""
            try:
                s_d = date.fromisoformat(start_str)
                e_d = date.fromisoformat(end_str)
            except ValueError as e:
                return {"action": "break_add", "ok": False, "error": f"日期格式错误: {e}"}
            if s_d > e_d:
                return {"action": "break_add", "ok": False,
                        "error": f"倒序区间: start {start_str} > end {end_str}"}
            self._guard()
            data = _load()
            entry = {"start": start_str, "end": end_str, "note": note}
            data.setdefault("breaks", []).append(entry)
            _save(data)
            return {"action": "break_add", "ok": True, "index": len(data["breaks"]) - 1,
                    "start": start_str, "end": end_str, "note": note, "total": len(data["breaks"])}
        if cmd == "remove" and len(parts) >= 2:
            try:
                idx = int(parts[1])
            except ValueError:
                return {"action": "break_remove", "ok": False, "error": f"无效序号: {parts[1]}"}
            self._guard()
            data = _load()
            breaks = data.get("breaks", [])
            if 0 <= idx < len(breaks):
                removed = breaks.pop(idx)
                _save(data)
                return {"action": "break_remove", "ok": True, "index": idx,
                        "removed": removed, "remaining": len(breaks)}
            return {"action": "break_remove", "ok": False, "error": f"序号越界: {idx}（共 {len(breaks)} 个区间）"}
        if cmd == "list":
            data = _load()
            return {"action": "break_list",
                    "manual_override": data.get("manual_override") or data.get("on_break", False),
                    "breaks": data.get("breaks", []), "count": len(data.get("breaks", [])),
                    "semester_end": (self._semester_dates()[1] or None).isoformat()
                    if self._semester_dates()[1] else None}
        if cmd == "clear":
            self._guard()
            if bp.exists():
                bp.unlink()
            return {"action": "break_clear", "ok": True, "message": "所有假期区间已清空"}
        if cmd == "status":
            data = _load()
            today = date.today()
            from schedule.day_plan import _on_break
            ss, se = self._semester_dates()
            on_break = _on_break(data, ss, se, today)
            source = "none"
            if on_break:
                if data.get("manual_override") or data.get("on_break"):
                    source = "manual_override"
                elif any(date.fromisoformat(b["start"]) <= today <= date.fromisoformat(b["end"])
                         for b in data.get("breaks", []) if b.get("start") and b.get("end")):
                    source = "break_range"
                elif ss and today < ss:
                    source = "semester_start"
                else:
                    source = "semester_end"
            return {"action": "break_status", "on_break": on_break, "source": source,
                    "manual_override": data.get("manual_override") or data.get("on_break", False),
                    "breaks": data.get("breaks", []),
                    "semester_end": (se or None).isoformat()
                    if se else None,
                    "semester_ended": se is not None
                    and today > se}
        return {"action": "break_error", "ok": False, "error": f"未知命令: {cmd_line}",
                "usage": "on|off|status|add YYYY-MM-DD YYYY-MM-DD [备注]|remove <序号>|list|clear"}
