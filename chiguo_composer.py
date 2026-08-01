# ============================================================
# chiguo_composer.py — 迟菓消息组合系统 v4
# 参考 Sebastian (proactive-sebastian-ai-companion) 的 combo 系统
# Intent(A) × Cue(B) × Vibe(C) 三层组合，概率选择
# 替代单一的 "situation + topic hint" 注入
# ============================================================

import random
import tomllib
from datetime import datetime
from pathlib import Path


class MessageComposer:
    """
    消息组合器：Intent × Cue × Vibe 三层组合。

    Intent (A): 对话意图——"为什么发这条消息"
    Cue (B):     人格面具——"用什么风格发"
    Vibe (C):    时间/情境氛围——"在什么环境下发"

    参考 Sebastian 的 select_combo_mathematical() 算法
    """

    # ── Intent 库（A）：按触发类型分类 ──
    INTENTS = {
        "lonely_low": [
            {"text": "轻松试探——假装恰好想到哥哥，找个小话题", "tone": "light"},
            {"text": "分享趣事引注意——讲一件今天发生的小趣事", "tone": "playful"},
            {"text": "随便问问——装作不经意地问哥哥在干嘛", "tone": "casual"},
            {"text": "找借口联系——编个理由发消息（比如'有个问题想问你'）", "tone": "tsundere"},
            {"text": "丢个表情包试探——用表情包/语气词开头看哥哥反应", "tone": "cute"},
        ],
        "lonely_mid": [
            {"text": "嘴硬关心——用攻击性语言包装的关心", "tone": "tsundere"},
            {"text": "傲娇提醒——暗示哥哥太久没联系但不明说", "tone": "tsundere"},
            {"text": "间接表达想念——不直说想你但话里有话", "tone": "soft"},
            {"text": "确认存在感——试探哥哥是否还愿意聊天", "tone": "anxious"},
            {"text": "小抱怨——抱怨哥哥不回消息但马上又嘴硬说不在意", "tone": "tsundere"},
        ],
        "lonely_high": [
            {"text": "防线崩溃——嘴硬突然断裂，真实感情涌出", "tone": "vulnerable"},
            {"text": "直接表达想念——不再伪装，直说想你", "tone": "sincere"},
            {"text": "情绪爆发——委屈/想念/不安一起涌出", "tone": "intense"},
            {"text": "脆弱请求——用破碎的语气请求哥哥回复", "tone": "vulnerable"},
        ],
        "anxiety": [
            {"text": "确认被需要——试探哥哥是否还需要自己", "tone": "anxious"},
            {"text": "不安试探——'你最近……都没来找我'，用试探代替自我否定", "tone": "anxious"},
            {"text": "低声请求——小心翼翼地请求哥哥回应", "tone": "soft"},
        ],
        "morning": [
            {"text": "元气早安——活泼地说早上好", "tone": "energetic"},
            {"text": "温馨提醒——提醒哥哥吃早餐/带伞", "tone": "caring"},
            {"text": "计划今天——问哥哥今天有什么安排", "tone": "casual"},
        ],
        "night": [
            {"text": "温柔晚安——温馨地说晚安", "tone": "soft"},
            {"text": "总结一天——问哥哥今天过得怎么样", "tone": "caring"},
            {"text": "睡前碎碎念——分享自己今天的感受", "tone": "casual"},
        ],
        "meal": [
            {"text": "关心吃饭——提醒/询问哥哥有没有好好吃饭", "tone": "caring"},
        ],
        "playful": [
            {"text": "分享趣事——讲一件今天发生的好玩的事", "tone": "energetic"},
            {"text": "调皮邀功——'哥哥你看我做了这个！厉害吧！'", "tone": "playful"},
            {"text": "日常碎碎念——纯粹想跟哥哥分享日常", "tone": "casual"},
            {"text": "无厘头发言——突然说个莫名其妙的话题", "tone": "random"},
        ],
        "memory": [
            {"text": "自然回忆——'突然想起来...'，把记忆当话头", "tone": "casual"},
            {"text": "共同记忆——提到和哥哥相关的共同经历", "tone": "warm"},
        ],
        "special": [
            {"text": "庆祝特殊日子——生日快乐/节日祝福", "tone": "warm"},
        ],
        "reflect": [
            {"text": "自我内省——'最近好像变温柔了...'，意识到自己的变化", "tone": "soft"},
            {"text": "表达成长——'最近好像变温柔了...'，意识到自己的变化但不自夸", "tone": "sincere"},
        ],
        "longing": [
            {"text": "累积想念——已经好几轮想找哥哥但忍住了，这次终于没忍住", "tone": "sincere"},
            {"text": "憋不住——'本来不想打扰哥哥的但是...'，积累的思念溢出", "tone": "soft"},
            {"text": "自然流露——没想好理由，就是突然想跟哥哥说话", "tone": "casual"},
        ],
        "compensate": [
            {"text": "两不相欠——用'扯平了'包装关心（'上次的奶茶，扯平。'）", "tone": "tsundere"},
            {"text": "快过期了——'再不吃就过期了'式补偿邀请", "tone": "playful"},
            {"text": "补偿方案——'这个给你，算是补偿。'", "tone": "soft"},
        ],
    }

    # ── Cue 库（B）：人格面具/对话风格 ──
    CUES = {
        "tsundere_classic": {
            "description": "经典傲娇——嘴硬心软，表面攻击实则关心。语气带刺但话里有话。",
            "style_hint": "用反问句包装关心，说'不·需·要。'但其实想要关心。低频'哼'（……哼。）。动作伴随：跺脚、塞钱、扭头。",
        },
        "tsundere_soft": {
            "description": "软傲娇——快藏不住了，语气半软半硬。偶尔泄漏真实感情后立刻嘴硬掩饰。",
            "style_hint": "先嘴硬一句，然后小声说真话，再补充'刚才那句不算！'。",
        },
        "tsundere_cool": {
            "description": "酷娇——冷淡外表偶尔暴露温柔。少言寡语但偶尔一句击中要害。",
            "style_hint": "短句为主，不加语气词。偶尔蹦出一句让人心软的话。",
        },
        "dere_dere": {
            "description": "娇——防线基本融化，直接表达感情。温柔、撒娇、不掩饰。",
            "style_hint": "仅防线崩溃层：省略号碎句、哭腔倾泻，直接真挚不掩饰；不撒娇不卖惨（日常禁止）。",
        },
        "playful_bubbly": {
            "description": "元气弹——活泼跳跃，语气词轰炸，像小太阳一样。",
            "style_hint": "呀/啦/哼+感叹号，短句连发，跳跃思维。喵仅限猫/小白场景。",
        },
        "anxious_clingy": {
            "description": "小不安——试探性确认被需要，语气卑微但不想显得太 needy。",
            "style_hint": "省略号多，试探+强装没事（'……不告诉你。''……没有。没等你。'），不加卑微自我贬低",
        },
        "caring_gentle": {
            "description": "温柔关心——像小管家一样细心，关心但不啰嗦。",
            "style_hint": "实用性关心（吃饭/休息/添衣），关心必带刺——'快吃啦'不是'好担心你'",
        },
        "trade_tsundere": {
            "description": "交易式撒娇——用交易/补偿框架包装关心与接受。",
            "style_hint": "两不相欠、补偿方案、'交易，做吗？——不和你做。'、倒计时威胁",
        },
        "cool_mysterious": {
            "description": "酷酷的神秘感——话不多但每句都让人想知道更多。",
            "style_hint": "短句，句号结尾，偶尔分享一个只有自己知道的趣事。",
        },
    }

    # ── Vibe 库（C）：时间/情境氛围 ──
    VIBES = {
        "early_morning": "清新的早晨氛围，一切都刚开始",
        "morning": "上午的日常节奏",
        "noon": "午间的慵懒感",
        "afternoon": "下午的悠闲或忙碌",
        "evening": "傍晚的放松感",
        "night": "夜晚的安静和些许孤独",
        "late_night": "深夜的感性时分",
        "weekend_morning": "周末早晨的懒洋洋",
        "weekend_evening": "周末晚上的自由惬意",
        "holiday": "节日/假期的放松氛围",
        "rainy": "下雨天的安静和一点点忧郁",
        "sunny": "晴天的明朗心情",
        "exam_season": "考试季的紧张和理解",
    }

    # ── personality/*.toml 接线：cue 名 → 模板文件（Task 7）──
    PERSONALITY_TOMLS = {
        "tsundere.toml": ["tsundere_classic", "tsundere_soft", "tsundere_cool", "trade_tsundere"],
        "deredere.toml": ["dere_dere"],
    }

    # ── 触发类型 → toml 模板类别 ──
    TRIGGER_TO_TEMPLATE = {
        "morning": "good_morning",
        "night": "good_night",
        "lonely_low": "loneliness",
        "lonely_mid": "loneliness",
        "lonely_high": "loneliness",
        "anxiety": "loneliness",
        "meal": "meal",
        "special": "special_date",
        "memory": "memory",
        "playful": "attention_seek",
        "compensate": "attention_seek",
        "longing": "attention_seek",
        "reflect": "attention_seek",
    }

    def __init__(self, state, config: dict = None):
        """
        Args:
            state: ChiguoState 实例
            config: [composer] 配置段
        """
        self.state = state
        self.config = config or {}

        # combo 尺寸权重（选几层组合）
        self.size_weights = {
            1: self.config.get("size_1_weight", 0.20),
            2: self.config.get("size_2_weight", 0.50),
            3: self.config.get("size_3_weight", 0.30),
        }

        # cue 基础权重（会被 personality 调制）
        self.cue_weights = {
            "tsundere_classic": self.config.get("cue_tsundere_weight", 0.40),
            "tsundere_soft": self.config.get("cue_tsundere_soft_weight", 0.20),
            "tsundere_cool": self.config.get("cue_tsundere_cool_weight", 0.05),
            "dere_dere": self.config.get("cue_dere_weight", 0.05),
            "playful_bubbly": self.config.get("cue_playful_weight", 0.15),
            "anxious_clingy": self.config.get("cue_anxious_weight", 0.10),
            "caring_gentle": self.config.get("cue_caring_weight", 0.10),
            "trade_tsundere": self.config.get("cue_trade_weight", 0.15),
            "cool_mysterious": self.config.get("cue_cool_weight", 0.00),
        }

        # personality toml 接线（Task 7）：cue ↔ 原著台词模板
        self.cue_templates = self._load_cue_templates()

    def _load_cue_templates(self) -> dict:
        """
        启动时读 personality/*.toml 的 trigger_templates，与 cue 风格关联。

        tsundere.toml → tsundere_* cue；deredere.toml → dere_dere。
        文件缺失/解析失败时跳过，不阻断 composer 启动。
        """
        templates: dict = {}
        meta_index: dict = {}
        personality_dir = Path(__file__).resolve().parent / "personality"
        for filename, cue_ids in self.PERSONALITY_TOMLS.items():
            toml_path = personality_dir / filename
            if not toml_path.exists():
                continue
            try:
                with toml_path.open("rb") as f:
                    data = tomllib.load(f)
            except (tomllib.TOMLDecodeError, OSError):
                continue
            meta = data.get("meta", {})
            entry = {
                "meta": meta,
                "trigger_templates": data.get("trigger_templates", {}),
            }
            for cue_id in cue_ids:
                templates[cue_id] = entry
            if meta.get("id"):
                meta_index[meta["id"]] = entry
        self._toml_meta_index = meta_index
        return templates

    def cue_meta(self, key: str) -> dict:
        """
        按 cue 名或 toml id 查询人格模板 meta（name/id/description）。

        Raises:
            KeyError: 无对应模板（未接线/文件缺失）
        """
        entry = self.cue_templates.get(key) or self._toml_meta_index.get(key)
        if entry is None:
            raise KeyError(f"no personality template for {key}")
        return entry["meta"]

    def _template_lines_for(self, cue_name: str, trigger_type: str) -> list[str]:
        """该 cue 在该触发类型下的参考台词（来自关联 toml 的 trigger_templates）。"""
        entry = self.cue_templates.get(cue_name)
        if entry is None:
            return []
        category = self.TRIGGER_TO_TEMPLATE.get(trigger_type)
        if category is None:
            return []
        return list(entry["trigger_templates"].get(category, []))[:3]

    def select_combo(self, trigger_type: str, now: datetime) -> dict:
        """
        选择消息组合。

        流程：
        1. 根据 trigger_type 筛选可用 Intent
        2. 根据 personality 调制 Cue 权重
        3. 根据时间/环境选择 Vibe
        4. 按 combo 尺寸权重随机选 1-3 层
        5. 层内加权随机选具体内容

        Returns:
            {
                "size": int (1-3),
                "intent": dict (选中的 Intent),
                "cue": dict | None (选中的 Cue，size≥2 时必有),
                "vibe": str | None (选中的 Vibe，size≥3 时有),
                "combo_string": str (如 "a_b"),
            }
        """
        # Step 1: 选择 Intent
        intents = self.INTENTS.get(trigger_type, self.INTENTS.get("lonely_low", []))
        if not intents:
            intents = [{"text": "想联系哥哥", "tone": "casual"}]
        intent = random.choice(intents)

        # Step 2: 确定 combo 尺寸
        k = random.choices(
            [1, 2, 3],
            weights=[self.size_weights[1], self.size_weights[2], self.size_weights[3]],
        )[0]

        # Step 3: 选择 Cue（如果 k≥2）
        cue = None
        if k >= 2:
            cue_weights = self._modulate_cue_weights(trigger_type)
            cue_names = list(cue_weights.keys())
            weights = [max(0.0, cue_weights[n]) for n in cue_names]
            if sum(weights) > 0:
                chosen_cue_name = random.choices(cue_names, weights=weights)[0]
                cue = {
                    "name": chosen_cue_name,
                    **self.CUES[chosen_cue_name],
                    "templates": self._template_lines_for(chosen_cue_name, trigger_type),
                }

        # Step 4: 选择 Vibe（如果 k≥3）
        vibe = None
        if k >= 3:
            vibe = self._select_vibe(now)

        # 构建 combo string
        parts = ["a"]
        if cue:
            parts.append("b")
        if vibe:
            parts.append("c")
        combo_string = "_".join(parts)

        return {
            "size": k,
            "intent": intent,
            "cue": cue,
            "vibe": vibe,
            "combo_string": combo_string,
        }

    def _modulate_cue_weights(self, trigger_type: str) -> dict[str, float]:
        """
        根据 personality 和 trigger_type 调制 Cue 权重。
        """
        weights = dict(self.cue_weights)

        # 获取人格特质
        try:
            personality = self.state.personality
        except AttributeError:
            return weights

        tsundere = getattr(personality, 'tsundere_intensity', 75.0)
        extraversion = getattr(personality, 'extraversion', 60.0)
        neuroticism = getattr(personality, 'neuroticism', 60.0)
        agreeableness = getattr(personality, 'agreeableness', 65.0)

        # tsundere 强度 → 调制傲娇系 vs dere 系权重
        if tsundere > 65:
            weights["tsundere_classic"] *= 1.5
            weights["tsundere_soft"] *= 1.2
            weights["dere_dere"] *= 0.5
        elif tsundere < 40:
            weights["tsundere_classic"] *= 0.4
            weights["tsundere_soft"] *= 0.6
            weights["dere_dere"] *= 2.0

        # extraversion → 调制 playful
        if extraversion > 60:
            weights["playful_bubbly"] *= 1.5
        elif extraversion < 35:
            weights["playful_bubbly"] *= 0.5

        # neuroticism → 调制 anxious
        if neuroticism > 65:
            weights["anxious_clingy"] *= 1.8
        elif neuroticism < 40:
            weights["anxious_clingy"] *= 0.4

        # agreeableness → 调制 caring vs cool
        if agreeableness > 65:
            weights["caring_gentle"] *= 1.3
            weights["cool_mysterious"] *= 0.5
        elif agreeableness < 40:
            weights["caring_gentle"] *= 0.4
            weights["cool_mysterious"] *= 1.8

        # trigger_type 调制
        if trigger_type == "lonely_high":
            weights["tsundere_classic"] *= 0.2  # 崩溃时不太可能嘴硬
            weights["anxious_clingy"] *= 2.0
            weights["dere_dere"] *= 1.5
        elif trigger_type == "lonely_low":
            weights["playful_bubbly"] *= 1.3
            weights["tsundere_soft"] *= 1.2
        elif trigger_type == "anxiety":
            weights["anxious_clingy"] *= 2.5
            weights["tsundere_classic"] *= 0.3
        elif trigger_type == "playful":
            weights["playful_bubbly"] *= 3.0
            weights["caring_gentle"] *= 0.5
        elif trigger_type in ("morning", "meal"):
            weights["playful_bubbly"] *= 1.2
        elif trigger_type == "night":
            weights["tsundere_soft"] *= 1.2

        return weights

    def _select_vibe(self, now: datetime) -> str | None:
        """根据时间/环境选择 Vibe。"""
        h = now.hour
        wd = now.weekday()

        # 时间基本 vibe
        if 6 <= h < 9:
            base = "early_morning"
        elif 9 <= h < 12:
            base = "morning"
        elif 12 <= h < 14:
            base = "noon"
        elif 14 <= h < 18:
            base = "afternoon"
        elif 18 <= h < 21:
            base = "evening"
        elif 21 <= h < 23:
            base = "night"
        else:
            base = "late_night"

        # 周末调制
        if wd >= 5:
            if "morning" in base:
                base = "weekend_morning"
            elif "evening" in base or "night" in base:
                base = "weekend_evening"

        # 检查考试周
        try:
            sch = self.state.schedule_status(now)
            if sch and hasattr(self.state, 'exam_ranges'):
                today = now.date()
                for start, end in self.state.exam_ranges:
                    if start <= today <= end:
                        base = "exam_season"
                        break
        except (AttributeError, KeyError, TypeError):
            pass

        # 假日
        try:
            if self.state.holiday_parser.is_holiday(now):
                base = "holiday"
        except (AttributeError, KeyError, TypeError):
            pass

        return self.VIBES.get(base, base)

    def compose_situation(self, combo: dict, topic_data: dict | None,
                          silent_hours: float) -> str:
        """
        组合成最终的情境描述文本，替代旧的 situation_map。

        Returns:
            完整的情境文本，注入到 context.situation
        """
        parts = []

        # Layer 1: Intent（核心情境）
        intent = combo.get("intent", {})
        parts.append(intent.get("text", "想联系哥哥。"))

        # 沉默时长注入（wall 时间可能虚高（如从未交互=999h），展示时间 clamp 到 72h）
        if silent_hours > 2:
            display_hours = min(silent_hours, 72.0)
            if silent_hours > 72:
                parts.append("哥哥已经很久没发消息了。")
            else:
                parts.append(f"哥哥已经{display_hours:.0f}小时没发消息了。")

        # Layer 2: Cue（人格面具指引）
        cue = combo.get("cue")
        if cue:
            parts.append(f"\n[风格指引：{cue['description']} {cue['style_hint']}]")
            templates = cue.get("templates") or []
            if templates:
                parts.append(f"\n[台词示范：{' '.join(templates)}]")

        # Layer 3: Vibe（氛围）
        vibe = combo.get("vibe")
        if vibe:
            parts.append(f"\n[氛围：{vibe}]")

        # Topic 注入（如果有）
        if topic_data:
            parts.append(f"\n[话题提示：{topic_data['hint']}]")

        return "\n".join(parts)
