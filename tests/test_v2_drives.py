"""tests/test_v2_drives.py — v2 驱动力评估 TDD。

Drive = 情绪/关系/未完成事项 → 「当前想做什么」。纯函数、确定性排序；
intensity ∈ [0,1]，<=0.05 过滤。对缺属性/NaN/None 输入容错（跳过该驱动，不崩）。
"""
import dataclasses
import math
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chiguo_time import CST  # noqa: E402
from domain.affect.model import AffectState  # noqa: E402
from domain.planning.drives import DriveDraft, evaluate_drives  # noqa: E402
from domain.planning.opportunities import OpportunityDraft  # noqa: E402

NOW = datetime(2026, 10, 3, 20, 0, tzinfo=CST)


def _opp(kind, **kw):
    base = dict(novelty=1.0, relevance=0.9, urgency=0.8,
                emotional_affordance=0.7, expires_at=None,
                observation_event_id=None, payload={})
    base.update(kw)
    return OpportunityDraft(kind=kind, **base)


def _low():
    """除被测维度外全部压到阈值以下：care≈0.04、playfulness=0、curiosity=0。"""
    return AffectState(loneliness=0.0, affection=5.0, energy=0.0)


def _rel(closeness=0.5, familiarity=0.0):
    from domain.relationship.model import RelationshipState
    return RelationshipState(closeness=closeness, familiarity=familiarity)


def _kinds(drafts):
    return [d.kind for d in drafts]


# ── 单条规则 ─────────────────────────────────────────────────────

def test_reconnect_from_loneliness():
    affect = AffectState(loneliness=40.0, affection=5.0, energy=0.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["reconnect"]
    assert drafts[0].intensity == pytest.approx(0.4)
    assert drafts[0].inputs == {"loneliness": 40.0}


def test_care_from_affection_and_closeness():
    affect = AffectState(loneliness=0.0, affection=80.0, energy=0.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(closeness=1.0),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["care"]
    assert drafts[0].intensity == pytest.approx(0.8)   # 0.8 × (0.5 + 1.0/2)
    assert drafts[0].inputs == {"affection": 80.0, "closeness": 1.0}


def test_playfulness_from_energy():
    affect = AffectState(loneliness=0.0, affection=5.0, energy=50.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["playfulness"]
    assert drafts[0].intensity == pytest.approx(0.2)   # 0.5 × 0.4
    assert drafts[0].inputs == {"energy": 50.0}


def test_unfinished_business_from_commitment_due_opportunity():
    affect = _low()
    drafts = evaluate_drives(affect=affect, relationship=_rel(),
                             opportunities=[_opp("commitment_due")], now=NOW)
    assert _kinds(drafts) == ["unfinished_business"]
    assert drafts[0].intensity == pytest.approx(0.8)
    inputs = drafts[0].inputs
    assert inputs["opportunity_kind"] == "commitment_due"
    assert inputs["count"] == 1
    assert inputs["best_score"] == pytest.approx(_opp("commitment_due").score)


def test_unfinished_business_absent_without_commitment_due():
    affect = _low()
    drafts = evaluate_drives(affect=affect, relationship=_rel(),
                             opportunities=[_opp("weather"), _opp("music")], now=NOW)
    assert drafts == []


def test_curiosity_from_familiarity():
    affect = AffectState(loneliness=0.0, affection=5.0, energy=0.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(familiarity=50.0),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["curiosity"]
    assert drafts[0].intensity == pytest.approx(0.15)  # 0.5 × 0.3
    assert drafts[0].inputs == {"familiarity": 50.0}


# ── 过滤 / 排序 / 组合 ───────────────────────────────────────────

def test_min_intensity_filter_boundary():
    """过滤条件为 intensity <= 0.05：恰好 0.05 被过滤，0.052 保留。"""
    affect = AffectState(loneliness=0.0, affection=5.0, energy=12.5)  # 0.05 → 过滤
    assert evaluate_drives(affect=affect, relationship=_rel(),
                           opportunities=[], now=NOW) == []
    affect = AffectState(loneliness=0.0, affection=5.0, energy=13.0)  # 0.052 → 保留
    drafts = evaluate_drives(affect=affect, relationship=_rel(),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["playfulness"]


def test_tie_break_by_kind_lexicographic():
    # reconnect = 20/100 = 0.2；playfulness = 50/100×0.4 = 0.2 → 同分按 kind 字典序
    affect = AffectState(loneliness=20.0, affection=5.0, energy=50.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["playfulness", "reconnect"]


def test_all_drives_composed_sorted_desc():
    affect = AffectState(loneliness=30.0, affection=70.0, energy=90.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(closeness=0.6,
                                                              familiarity=40.0),
                             opportunities=[_opp("commitment_due")], now=NOW)
    assert _kinds(drafts) == [
        "unfinished_business",  # 0.8
        "care",                 # 0.70 × (0.5+0.3) = 0.56
        "playfulness",          # 0.90 × 0.4 = 0.36
        "reconnect",            # 0.30
        "curiosity",            # 0.40 × 0.3 = 0.12
    ]
    assert all(isinstance(d, DriveDraft) for d in drafts)
    assert all(0.0 <= d.intensity <= 1.0 for d in drafts)


def test_drive_draft_is_frozen():
    drafts = evaluate_drives(affect=AffectState(loneliness=40.0),
                             relationship=_rel(), opportunities=[], now=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        drafts[0].intensity = 0.0


# ── 容错 ─────────────────────────────────────────────────────────

def test_missing_attributes_skip_only_that_drive():
    class _PartialAffect:
        loneliness = 30.0          # 缺 affection / energy

    class _PartialRel:
        closeness = 0.5            # 缺 familiarity

    drafts = evaluate_drives(affect=_PartialAffect(), relationship=_PartialRel(),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["reconnect"]


def test_none_inputs_tolerated():
    assert evaluate_drives(affect=None, relationship=None,
                           opportunities=None, now=NOW) == []
    drafts = evaluate_drives(affect=None, relationship=None,
                             opportunities=[_opp("commitment_due")], now=NOW)
    assert _kinds(drafts) == ["unfinished_business"]


def test_nan_and_inf_inputs_skip_drives():
    affect = AffectState(loneliness=float("nan"), affection=float("inf"),
                         energy=50.0)
    drafts = evaluate_drives(affect=affect, relationship=_rel(familiarity=float("nan")),
                             opportunities=[], now=NOW)
    assert _kinds(drafts) == ["playfulness"]
    assert drafts[0].intensity == pytest.approx(0.2)
    assert math.isfinite(drafts[0].intensity)
