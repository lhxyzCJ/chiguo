"""tests/test_v2_planning.py — v2 planning 层（opportunity 发现）TDD。

Opportunity = 世界观测 × 状态推导出的「可行动契机」；只描述时机与依据，
不做发送决策（那是 planner 的事）。纯函数、无 IO、确定性输出顺序。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from domain.planning.opportunities import OpportunityDraft, discover_opportunities  # noqa: E402
from sources.base import Observation  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 3, 20, 0, tzinfo=CST)


def _obs(type, payload, *, expires_at=None, observed_at=None):
    return Observation(type=type, source=type.split(".")[0],
                       observed_at=observed_at or NOW,
                       expires_at=expires_at, payload=payload)


class _Row:
    """构造用户侧行（duck-type 仓储行：commitments / threads）。"""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_anniversary_today_creates_opportunity():
    obs = [_obs("anniversary.upcoming", {"name": "迟菓生日", "date": "2026-10-03",
                                        "days_until": 0})]
    drafts = discover_opportunities(observations=obs, commitments=[], threads=[], now=NOW)
    assert len(drafts) == 1
    d = drafts[0]
    assert d.kind == "anniversary"
    assert d.relevance >= 0.8 and d.emotional_affordance >= 0.7
    assert d.payload["name"] == "迟菓生日"


def test_future_anniversary_and_other_observations():
    obs = [
        _obs("anniversary.upcoming", {"name": "生日", "days_until": 3}),
        _obs("weather.changed", {"condition": "雨", "temperature": 18}),
        _obs("music.observed", {"plays": [{"playTime": 1}]}),
        _obs("schedule.state", {"in_class": False, "class_load": "free"}),
    ]
    drafts = discover_opportunities(observations=obs, commitments=[], threads=[], now=NOW)
    kinds = {d.kind for d in drafts}
    assert kinds == {"anniversary", "weather", "music"}  # schedule.state 只作环境事实
    weather = next(d for d in drafts if d.kind == "weather")
    assert weather.relevance < 0.5  # 天气是 secondary cue，不是主机会


def test_expired_observation_skipped():
    obs = [_obs("anniversary.upcoming", {"name": "生日", "days_until": 0},
                expires_at=NOW - timedelta(minutes=1))]
    assert discover_opportunities(observations=obs, commitments=[], threads=[],
                                  now=NOW) == []


def test_due_commitment_creates_opportunity():
    due = _Row(id="c1", kind="user_event", subject="线代考试", status="open",
               due_at=datetime(2026, 10, 3, 9, 0, tzinfo=CST))
    not_due = _Row(id="c2", kind="user_event", subject="交作业", status="open",
                   due_at=NOW + timedelta(hours=5))
    resolved = _Row(id="c3", kind="user_event", subject="体检", status="done",
                    due_at=NOW - timedelta(hours=1))
    drafts = discover_opportunities(observations=[], commitments=[due, not_due, resolved],
                                    threads=[], now=NOW)
    assert [d.kind for d in drafts] == ["commitment_due"]
    assert drafts[0].payload["commitment_id"] == "c1"
    assert drafts[0].urgency >= 0.6


def test_due_commitment_expires_with_grace():
    """到期承诺有宽限窗（默认 24h）：超过 24h 仍未解决的旧承诺不再产机会。"""
    stale = _Row(id="c9", kind="user_event", subject="旧事", status="open",
                 due_at=NOW - timedelta(hours=30))
    drafts = discover_opportunities(observations=[], commitments=[stale],
                                    threads=[], now=NOW)
    assert drafts == []


def test_stale_open_thread_creates_opportunity():
    stale = _Row(id="t1", subject="考试怎么样", state="open", opened_at=NOW - timedelta(days=1),
                 last_interaction_at=NOW - timedelta(hours=8), source="conversation")
    fresh = _Row(id="t2", subject="新话题", state="open", opened_at=NOW,
                 last_interaction_at=NOW - timedelta(minutes=10), source="conversation")
    drafts = discover_opportunities(observations=[], commitments=[],
                                    threads=[stale, fresh], now=NOW)
    assert [d.kind for d in drafts] == ["open_thread"]
    assert drafts[0].payload["thread_id"] == "t1"


def test_output_is_deterministically_ordered():
    obs = [
        _obs("music.observed", {"plays": []}),
        _obs("anniversary.upcoming", {"name": "生日", "days_until": 0}),
        _obs("weather.changed", {"condition": "晴"}),
    ]
    a = discover_opportunities(observations=obs, commitments=[], threads=[], now=NOW)
    b = discover_opportunities(observations=list(reversed(obs)), commitments=[],
                               threads=[], now=NOW)
    assert [d.kind for d in a] == [d.kind for d in b]
    # 高分在前（anniversary 分数最高）
    assert a[0].kind == "anniversary"


# ── planner ─────────────────────────────────────────────────────

from domain.planning.planner import Defer, IntentDraft, Wait, plan  # noqa: E402


def _opp(kind, **kw):
    base = dict(novelty=1.0, relevance=0.5, urgency=0.2,
                emotional_affordance=0.3, expires_at=None,
                observation_event_id=None, payload={})
    base.update(kw)
    return OpportunityDraft(kind=kind, **base)


def test_plan_commitment_due_becomes_follow_up():
    opp = _opp("commitment_due", relevance=0.9, urgency=0.8,
               emotional_affordance=0.7,
               payload={"commitment_id": "c1", "subject": "线代考试"})
    decision = plan(opportunities=[opp], drives=[], constraints=None, now=NOW)
    assert isinstance(decision, IntentDraft)
    assert decision.type == "follow_up"
    assert decision.why["opportunity_kind"] == "commitment_due"
    assert decision.why["opportunity_payload"]["commitment_id"] == "c1"


def test_plan_anniversary_becomes_celebrate():
    opp = _opp("anniversary", relevance=0.9, urgency=0.6,
               emotional_affordance=0.8, payload={"name": "生日"})
    decision = plan(opportunities=[opp], drives=[], constraints=None, now=NOW)
    assert isinstance(decision, IntentDraft)
    assert decision.type == "celebrate"


def test_plan_weather_alone_waits():
    """scenario 3：天气只能作 secondary cue，单独不足以触发主动消息。"""
    weather = _opp("weather", relevance=0.4, urgency=0.1, emotional_affordance=0.3,
                   payload={"condition": "雨"})
    decision = plan(opportunities=[weather], drives=[], constraints=None, now=NOW)
    assert isinstance(decision, Wait)
    assert decision.reason == "no_eligible_opportunity"


def test_plan_no_opportunities_waits():
    assert isinstance(plan(opportunities=[], drives=[], constraints=None, now=NOW), Wait)


def test_plan_weather_as_secondary_cue():
    """主机会（承诺到期）+ 天气 → 天气进 secondary，不作为 primary。"""
    due = _opp("commitment_due", relevance=0.9, urgency=0.8,
               emotional_affordance=0.7, payload={"commitment_id": "c1"})
    weather = _opp("weather", relevance=0.4, urgency=0.1,
                   emotional_affordance=0.3, payload={"condition": "雨"})
    decision = plan(opportunities=[due, weather], drives=[], constraints=None, now=NOW)
    assert isinstance(decision, IntentDraft)
    assert decision.why["opportunity_kind"] == "commitment_due"
    assert [s["kind"] for s in decision.plan["secondary"]] == ["weather"]


def test_plan_quiet_defers_with_candidate():
    due = _opp("commitment_due", relevance=0.9, urgency=0.8,
               emotional_affordance=0.7, payload={"commitment_id": "c1"})
    until = NOW + timedelta(hours=10)
    decision = plan(opportunities=[due], drives=[], now=NOW,
                    constraints={"quiet": True, "quiet_until": until})
    assert isinstance(decision, Defer)
    assert decision.until == until
    assert decision.reason == "quiet_hours"
    assert isinstance(decision.candidate, IntentDraft)


def test_plan_records_drives_in_why():
    due = _opp("commitment_due", relevance=0.9, urgency=0.8,
               emotional_affordance=0.7, payload={"commitment_id": "c1"})
    drives = [_Row(kind="care", intensity=0.8), _Row(kind="reconnect", intensity=0.2)]
    decision = plan(opportunities=[due], drives=drives, constraints=None, now=NOW)
    assert decision.why["drives"] == ["care"]  # 只记强度达阈的驱动力
