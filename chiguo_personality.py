# ============================================================
# chiguo_personality.py — 迟菓多维人格系统 v4
# 参考 soulforge (digital-companion-core) 的 Big Five + 角色特有维度
# 人格与情绪正交但相互作用：情绪快速变化，人格缓慢演变
# ============================================================

from dataclasses import dataclass, asdict


@dataclass
class PersonalityTraits:
    """
    迟菓的多维人格特质。0-100 量表。
    大五人格 (Big Five) + 角色特有维度。

    人格是"慢变量"——每次互动变化 <0.2，经数周/月才显着变化。
    情绪是"快变量"——分钟/小时级别波动。
    """

    # ── 大五人格 ──
    openness: float = 55.0
    """开放性：高→愿意分享内心、主动找话题、尝试新表达方式"""

    conscientiousness: float = 65.0
    """尽责性：高→更关心主人细节（课表、健康）、更准时（早安晚安）"""

    extraversion: float = 60.0
    """外向性：高→更活泼、话多、语气词多、主动发起互动"""

    agreeableness: float = 65.0
    """宜人性：高→更温柔、更容易妥协、更少嘴硬攻击"""

    neuroticism: float = 60.0
    """神经质：高→更容易焦虑、敏感、情绪波动大、防线更容易崩溃"""

    # ── 角色特有维度 ──
    tsundere_intensity: float = 75.0
    """傲娇强度：高→嘴更硬、更不愿直接表达感情。替代旧版 tsundere_index"""

    playfulness: float = 55.0
    """贪玩程度：高→更容易触发 playful 触发、分享趣事、调皮捣蛋"""

    attachment_style: float = 60.0
    """依恋风格：高→焦虑型依恋（怕被抛弃、需要确认被需要）；
       低→回避型依恋（假装不在乎、保持距离）"""

    def __post_init__(self):
        # 基线：构造时实际传入的初始值（非 dataclass 默认值），回归目标。
        # 非 dataclass 字段 → asdict/__eq__ 不包含。
        self._baseline = {
            field_name: getattr(self, field_name)
            for field_name in self.__dataclass_fields__
        }

    def reset_baseline(self, baseline: dict):
        """覆盖基线（加载状态时恢复持久化的初始值，防止基线随漂移状态漂移）。"""
        self._baseline = {
            field_name: float(baseline[field_name])
            for field_name in self.__dataclass_fields__
            if field_name in baseline
        }

    def regress_to_baseline(self, rate: float = 0.01):
        """向初始基线软回归，防人格漂移。rate=0 关闭。"""
        if rate <= 0:
            return
        for field_name in self.__dataclass_fields__:
            val = getattr(self, field_name)
            base = self._baseline.get(field_name)
            if base is not None:
                setattr(self, field_name, val + (base - val) * rate)

    def clamp(self):
        """钳位到 [10, 90]（人格不会极端到 0 或 100）。"""
        for field_name in self.__dataclass_fields__:
            val = getattr(self, field_name)
            setattr(self, field_name, max(10.0, min(90.0, val)))

    def evolve(self, delta: 'PersonalityDelta'):
        """应用人格变化增量，自动钳位。只应用非零字段。"""
        for field_name in self.__dataclass_fields__:
            change = getattr(delta, field_name, 0.0)
            if change != 0.0:
                current = getattr(self, field_name)
                setattr(self, field_name, current + change)
        self.clamp()

    def dominant_profile(self) -> str:
        """
        返回主导人格画像，用于 _build_context 增强 layer_guidance。
        基于最突出的维度组合。
        """
        profiles = []

        if self.tsundere_intensity > 65:
            profiles.append("tsundere_heavy")
        elif self.tsundere_intensity < 40:
            profiles.append("tsundere_light")

        if self.extraversion > 60:
            profiles.append("extraverted")
        elif self.extraversion < 35:
            profiles.append("introverted")

        if self.neuroticism > 65:
            profiles.append("sensitive")
        elif self.neuroticism < 40:
            profiles.append("stable")

        if self.agreeableness > 65:
            profiles.append("gentle")
        elif self.agreeableness < 40:
            profiles.append("sharp")

        if self.playfulness > 65:
            profiles.append("playful")

        if self.attachment_style > 65:
            profiles.append("anxious_attachment")
        elif self.attachment_style < 40:
            profiles.append("avoidant_attachment")

        if not profiles:
            profiles.append("balanced")

        return "|".join(profiles)

    def anxiety_sensitivity(self) -> float:
        """neuroticism 对不安变化的敏感度。0.8~1.3。"""
        return 0.8 + (self.neuroticism / 100) * 0.5

    def openness_bonus(self) -> float:
        """openness 对话题多样性的加成。1.0~2.0。"""
        return 1.0 + (self.openness / 100)


# ── 人格变化增量 ──

@dataclass
class PersonalityDelta:
    """
    人格变化增量。所有字段默认 0.0（无变化）。
    与 PersonalityTraits 不同，此 dataclass 用于表示每次交互的微量调整。
    """
    openness: float = 0.0
    conscientiousness: float = 0.0
    extraversion: float = 0.0
    agreeableness: float = 0.0
    neuroticism: float = 0.0
    tsundere_intensity: float = 0.0
    playfulness: float = 0.0
    attachment_style: float = 0.0

    def evolve(self, other: 'PersonalityDelta'):
        """累加另一个增量的非零字段。"""
        for field_name in self.__dataclass_fields__:
            change = getattr(other, field_name, 0.0)
            if change != 0.0:
                current = getattr(self, field_name)
                setattr(self, field_name, current + change)
        return self


# ── 人格变化预设 ──
# 每次互动的最大变化量。极小，确保人格缓慢演变。

class PersonalityDeltas:
    """预定义的人格变化增量预设。"""

    # 正面互动
    WARM_REPLY = PersonalityDelta(
        agreeableness=0.1, neuroticism=-0.08,
        attachment_style=0.05, tsundere_intensity=-0.05,
    )
    FAST_REPLY = PersonalityDelta(
        extraversion=0.05, attachment_style=0.08,
        tsundere_intensity=-0.08, playfulness=0.03,
    )
    LONG_MESSAGE = PersonalityDelta(
        openness=0.05, conscientiousness=0.03,
        agreeableness=0.05,
    )

    # 负面互动
    SLOW_REPLY = PersonalityDelta(
        attachment_style=-0.08, neuroticism=0.08,
        tsundere_intensity=0.05,
    )
    COLD_REPLY = PersonalityDelta(
        neuroticism=0.12, agreeableness=-0.05,
        tsundere_intensity=0.08, attachment_style=-0.05,
    )
    VERY_SLOW_REPLY = PersonalityDelta(
        neuroticism=0.15, attachment_style=-0.1,
        tsundere_intensity=0.1, agreeableness=-0.03,
    )

    # 自己的行为
    SENT_AND_REPLIED = PersonalityDelta(
        playfulness=0.05, extraversion=0.03,
        attachment_style=0.03, neuroticism=-0.05,
    )
    SENT_NO_REPLY = PersonalityDelta(
        tsundere_intensity=0.1, neuroticism=0.08,
        extraversion=-0.03,
    )


# ── 默认人格（迟菓初始设定）──

def default_personality() -> PersonalityTraits:
    """返回迟菓的默认初始人格。"""
    return PersonalityTraits(
        openness=55.0,
        conscientiousness=65.0,
        extraversion=60.0,
        agreeableness=65.0,
        neuroticism=60.0,
        tsundere_intensity=75.0,
        playfulness=55.0,
        attachment_style=60.0,
    )


# ── 序列化工具 ──

def personality_to_dict(p: PersonalityTraits) -> dict:
    """转为可 JSON 序列化的字典。"""
    return asdict(p)


def personality_from_dict(d: dict) -> PersonalityTraits:
    """从字典恢复。缺失字段用默认值。"""
    defaults = asdict(default_personality())
    defaults.update(d)
    return PersonalityTraits(**{k: defaults[k] for k in PersonalityTraits.__dataclass_fields__})
