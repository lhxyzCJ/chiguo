"""app.runtime.extractor — message.received → 结构化事件（确定性规则提取）。

Phase 6 前的最小提取器：中文自然语言 → commitment.created 事件，规则驱动、
无 LLM（LLM 提取留给 Phase 7 Pi）。提取是旁路：逐条消费、失败记 stderr 并继续，
任何情况下不向主链抛出。

游标语义与 reducer 相同：runtime_checkpoints.stream="extractor" 记 rowid（提交序），
按 `rowid > last_cursor` 增量消费 message.received；
`runtime_checkpoints` 表不存在（migration 未跑）→ 返回空列表空转，不抛。

规则（最小确定性集）：
1. 日期词：今天/明天/后天/大后天、N天后、下周[一二三四五六日天]、
   周[一二三四五六日天]（本周，若已过则下周）、M月D日/号（今年已过则明年）。
   取最左命中的一个；只取日期，时间固定 09:00 CST；解析失败不产事件。
2. 命中日期词且命中事件触发词（TRIGGER_WORDS，先剥离易混词 EXCLUDE_WORDS，
   如「考虑/思考/参考/交流/还好」）→ 产 commitment.created
   （source="extractor"，extracted_by="rule"），causation_id=原 message.received，
   correlation_id 继承原事件。
3. 已过期的日期（解析出的 due ≤ now）不产事件——防过去式闲谈误报
   （如 20:00 说「今天考试好难啊」不应产生今天的承诺）。
4. 幂等：同一 (subject, due_at) 24h 内已有 commitment.created → 跳过（防 bridge 重报）；
   游标保证每条 message.received 只处理一次。
"""
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta

from chiguo_time import CST
from storage.events import EventStore
from storage.repositories.checkpoints import CheckpointRepo

STREAM = "extractor"

_DEFAULT_HOUR = 9          # 只取日期，时间默认 09:00
_SUBJECT_MAX = 40          # subject 截断长度
_DEDUP_SCAN = 50           # 幂等检查扫描的近期 commitment.created 条数
_DEDUP_WINDOW = timedelta(hours=24)

# 事件触发词（命中其一即可）
TRIGGER_WORDS = (
    "考试", "考", "面试", "交", "提交", "答辩", "体检", "开会", "约", "聚",
    "面签", "生日", "纪念日", "报名", "缴费", "还", "取", "寄",
)
_TRIGGER_RE = re.compile("|".join(map(re.escape, TRIGGER_WORDS)))

# 易混词：单字触发词（考/交/还/取）常见于非事件语境，匹配前先剥离
# （注：「约会」是真实事件，不在剥离表内）
EXCLUDE_WORDS = ("考虑", "思考", "参考", "交流", "还好", "还是", "还有", "取消")

_REL_DAYS = {"今天": 0, "明天": 1, "后天": 2, "大后天": 3}
_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

# 日期词单一正则（取最左命中；大后天/下周 等长词优先，避免前缀吞并）
_DATE_RE = re.compile(
    r"(?P<rel>大后天|后天|今天|明天)"
    r"|(?P<n_days>\d+)天后"
    r"|下周(?P<next_week>[一二三四五六日天])"
    r"|(?P<this_week>周[一二三四五六日天])"
    r"|(?P<month>\d{1,2})月(?P<day>\d{1,2})[日号]"
)

# subject 清洗的首尾裁剪集（中英文标点 + 空白）
_STRIP_CHARS = (
    " \t\r\n　"
    "!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~"
    "。，、；：？！…—·～「」『』【】《》〈〉（）〔〕“”‘’"
)


def _warn(message: str) -> None:
    """旁路告警：提取链不阻断主链，只记 stderr。"""
    print(f"[extractor] {message}", file=sys.stderr)


def _parse_dt(value) -> datetime | None:
    """宽松时间解析（游标回写用）：非法 → None。"""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _parse_due(text: str, now: datetime) -> tuple[datetime, str] | None:
    """解析日期词 → (due_at, 命中的日期词文本)；无日期/非法日期 → None。

    多个日期词取最左（最先出现）的一个；时区 CST、时间固定 09:00。
    周X 语义：本周该日未过则本周，已过则下周；M月D日 今年已过则明年。
    """
    m = _DATE_RE.search(text)
    if m is None:
        return None
    token = m.group(0)
    today = now.date()
    try:
        rel = m.group("rel")
        if rel is not None:
            day = today + timedelta(days=_REL_DAYS[rel])
        elif m.group("n_days") is not None:
            day = today + timedelta(days=int(m.group("n_days")))
        elif m.group("next_week") is not None:
            monday = today - timedelta(days=today.weekday())
            day = monday + timedelta(days=7 + _WEEKDAYS[m.group("next_week")])
        elif m.group("this_week") is not None:
            monday = today - timedelta(days=today.weekday())
            day = monday + timedelta(days=_WEEKDAYS[m.group("this_week")[1]])
            if day < today:
                day += timedelta(days=7)
        else:
            day = date(now.year, int(m.group("month")), int(m.group("day")))
            if day < today:
                day = date(now.year + 1, int(m.group("month")), int(m.group("day")))
    except (ValueError, OverflowError):
        return None  # 非法日期（如 2月30日）/ 天数溢出
    return datetime(day.year, day.month, day.day, _DEFAULT_HOUR, 0, tzinfo=CST), token


def _clean_subject(text: str, token: str) -> str:
    """去掉日期词本身、去首尾标点/空白，截断 40 字符；清洗后为空 → 原文截断。"""
    cleaned = text.replace(token, "", 1).strip(_STRIP_CHARS)
    if not cleaned:
        cleaned = text.strip(_STRIP_CHARS)
    return cleaned[:_SUBJECT_MAX]


class Extractor:
    """确定性提取器：catch_up() 增量消费 message.received；当前状态只含游标。"""

    STREAM = STREAM

    def __init__(self, db):
        self.db = db
        self.events = EventStore(db)
        self.checkpoints = CheckpointRepo(db)

    def catch_up(self, now: datetime, limit: int = 200) -> list[str]:
        """消费游标之后的 message.received，返回本轮新产生的 commitment.created id。

        每条消息只处理一次；单条失败记 stderr 并继续；游标随本轮读到的最后一条
        消息前进（坏行不卡死）。异常绝不外抛。
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 0
        if limit <= 0:
            return []
        try:
            cp = self.checkpoints.get(self.STREAM)
            try:
                cursor = int(cp.last_event_id) if cp is not None and cp.last_event_id else 0
            except (TypeError, ValueError):
                cursor = 0
            rows = self.db.connect().execute(
                "SELECT rowid AS _rowid, event_id, payload, correlation_id,"
                " occurred_at FROM events"
                " WHERE type = 'message.received' AND rowid > ?"
                " ORDER BY rowid ASC LIMIT ?",
                (cursor, limit)).fetchall()
        except sqlite3.OperationalError:
            return []  # runtime_checkpoints/events 表不存在（migration 未跑）→ 空转
        except Exception as e:  # noqa: BLE001 —— 提取不上主链
            _warn(f"读取失败，本轮空转: {e!r}")
            return []

        produced: list[str] = []
        last_cursor: int | None = None
        last_at = None
        for row in rows:
            last_cursor, last_at = row["_rowid"], row["occurred_at"]
            try:
                event_id = self._extract(row, now)
            except Exception as e:  # noqa: BLE001 —— 坏数据逐条跳过
                _warn(f"{row['event_id']} 提取失败，跳过: {e!r}")
                continue
            if event_id is not None:
                produced.append(event_id)

        if last_cursor is not None:
            try:
                self.checkpoints.save(self.STREAM, last_event_id=str(last_cursor),
                                      last_occurred_at=_parse_dt(last_at), state={})
            except Exception as e:  # noqa: BLE001
                _warn(f"游标写盘失败（下轮重放，由幂等去重兜底）: {e!r}")
        return produced

    # ── 单条消息提取 ─────────────────────────────────────

    def _extract(self, row, now: datetime) -> str | None:
        payload = self._parse_payload(row["payload"])
        if payload is None:
            return None
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        parsed = _parse_due(text, now)
        if parsed is None:
            return None
        if parsed[0] <= now:
            return None  # 已过期日期：过去式闲谈不产承诺（如 20:00 说「今天考试好难」）
        match_text = text
        for w in EXCLUDE_WORDS:
            match_text = match_text.replace(w, "")
        if _TRIGGER_RE.search(match_text) is None:
            return None
        due_at, token = parsed
        subject = _clean_subject(text, token)
        if not subject:
            return None
        if self._is_recent_duplicate(subject, due_at, now):
            return None
        ev = self.events.append(
            "commitment.created", source="extractor",
            payload={
                "kind": "user_event",
                "subject": subject,
                "due_at": due_at.isoformat(),
                "details": {"text": text},
                "extracted_by": "rule",
            },
            occurred_at=now, observed_at=now,
            causation_id=row["event_id"], correlation_id=row["correlation_id"])
        return ev.event_id

    @staticmethod
    def _parse_payload(raw):
        """事件 payload 解码：非法 JSON 抛出交上层逐条容错；非 dict（含 null）→ None。"""
        if isinstance(raw, dict):
            return raw
        if raw is None or raw == "":
            return None
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None

    def _is_recent_duplicate(self, subject: str, due_at: datetime,
                             now: datetime) -> bool:
        """同 (subject, due_at) 且 24h 内已有 commitment.created → True（幂等跳过）。"""
        try:
            recent = self.events.recent(type="commitment.created", limit=_DEDUP_SCAN)
        except Exception as e:  # noqa: BLE001 —— 坏数据不阻断新提取（宁可重复不可沉默）
            _warn(f"幂等检查失败，按新事件处理: {e!r}")
            return False
        due_iso = due_at.isoformat()
        for ev in recent:
            payload = ev.payload if isinstance(ev.payload, dict) else {}
            if payload.get("subject") != subject or payload.get("due_at") != due_iso:
                continue
            try:
                within = (abs((now - ev.occurred_at).total_seconds())
                          <= _DEDUP_WINDOW.total_seconds())
            except (TypeError, ValueError):
                within = True  # 时间不可比 → 保守视为窗口内（防 bridge 重报优先）
            if within:
                return True
        return False
