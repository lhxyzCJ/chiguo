# ============================================================
# chiguo_topics.py — 迟菓话题选择器
# 为孤独触发（lonely_low/mid）提供自然破冰话题。
# 8 个来源(新增 netease)：课表/假期、mem0 记忆、季节感知、通用关心、
#           节气、纪念日/倒计时、偏好追问、网易云音乐。
# 0 token 消耗，0 新依赖。
# ============================================================

import random
from datetime import datetime

from chiguo_math import weighted_trigger_choice, in_quiet_window, jaccard_3gram
from chiguo_state import emotion_tag_snapshot
from solar_terms import SolarTerms


class TopicPicker:
    """话题选择器：从可用来源加权随机选取一个自然话题。"""

    def __init__(self, state, config: dict, netease_service=None,
                 recent_sent_texts: list[str] | None = None):
        """
        Args:
            state: ChiguoState 实例（访问 schedule/holiday/memory 数据面）
            config: chiguo_proactive.toml 的 [topic_picker] 段
            netease_service: 网易云策略层(NeteaseService 实例)，可为 None(降级)
            recent_sent_texts: A9 最近已发消息文本列表（内容级防复读数据源，
                由调用方从发送日志读取注入；None/空 = 不查重，行为同旧版）
        """
        self.state = state
        self.netease_service = netease_service  # v9: 网易云策略层,可为 None(降级)
        # ── A9: 内容级防复读参数（[topic_picker] 段）──
        self.repeat_jaccard_threshold = config.get("repeat_jaccard_threshold", 0.6)
        self.repeat_history_n = int(config.get("repeat_history_n", 5))
        self.recent_sent_texts = list(recent_sent_texts or [])[:self.repeat_history_n]
        self.weights = {
            "schedule": config.get("schedule_weight", 0.30),
            "memory": config.get("memory_weight", 0.25),
            "weather_season": config.get("weather_season_weight", 0.20),
            "general": config.get("general_weight", 0.25),
            "solar_terms": config.get("solar_terms_weight", 0.10),
            "anniversary": config.get("anniversary_weight", 0.15),
            "preference_followup": config.get("preference_followup_weight", 0.10),
            "netease": config.get("netease_weight", 0.12),  # v9
        }
        self.solar_terms = SolarTerms()

    def pick(self, now: datetime) -> dict | None:
        """
        加权随机选取一个话题。general 永远可用，所以总是返回有效结果。
        情绪快速变化时调权重：偏向关心型话题，降低轻松型话题。
        v4: 人格调制话题多样性（高开放性→更多 memory/anniversary）
        """
        # 变化率检查
        lo_rate = getattr(self.state.emotion, 'loneliness_rate', 0.0)
        anx_rate = getattr(self.state.emotion, 'anxiety_rate', 0.0)
        high_rate = lo_rate > 3.0 or anx_rate > 2.0

        weights = dict(self.weights)
        if high_rate:
            weights["general"] *= 1.5
            weights["schedule"] *= 1.3
            weights["weather_season"] *= 0.7
            weights["solar_terms"] *= 0.6

        # ── v4: 人格调制 ──
        try:
            pers = self.state.personality
            openness_bonus = pers.openness_bonus()  # 1.0~2.0
            # 高开放性 → 更多 memory 和 anniversary 话题
            weights["memory"] *= openness_bonus
            weights["anniversary"] *= openness_bonus
            # 低开放性（内向/谨慎）→ 更多 schedule 和 general
            if openness_bonus < 1.3:
                weights["schedule"] *= 1.3
                weights["general"] *= 1.2
        except AttributeError:
            pass

        candidates = []

        sched = self._schedule_topic(now)
        if sched:
            candidates.append({"topic": sched, "weight": weights["schedule"]})

        mem = self._memory_topic()
        if mem:
            candidates.append({"topic": mem, "weight": weights["memory"]})

        st = self._solar_terms_topic(now)
        if st:
            candidates.append({"topic": st, "weight": weights["solar_terms"]})

        ann = self._anniversary_topic(now)
        if ann:
            candidates.append({"topic": ann, "weight": weights["anniversary"]})

        pref = self._preference_followup_topic(now)
        if pref:
            candidates.append({"topic": pref, "weight": weights["preference_followup"]})

        netease = self._netease_music_topic(now)
        if netease:
            candidates.append({"topic": netease, "weight": weights["netease"]})

        candidates.append({
            "topic": self._weather_season_topic(now),
            "weight": weights["weather_season"],
        })
        candidates.append({
            "topic": self._general_topic(now),
            "weight": weights["general"],
        })

        # ── A9: 内容级防复读——候选与最近已发消息查重,高相似候选弃用 ──
        # 只作用于 topic 候选选择层（生成侧内容多样性），不改 daemon 发不发决策。
        # 全部候选被弃用 → topic 空注入（返回 None）。
        if self.recent_sent_texts:
            candidates = [
                c for c in candidates
                if not self._is_repeat(c["topic"].get("hint", ""))
            ]
        if not candidates:
            return None

        chosen = weighted_trigger_choice(candidates)
        topic = chosen["topic"] if chosen else None
        # v9: netease 候选被选中 → 确认消费配额(peek 不消费,抽选后补)
        if topic and self.netease_service and topic.get("type") in ("netease_music", "netease_fault"):
            try:
                if topic.get("type") == "netease_fault":
                    self.netease_service.consume_fault_topic(now)
                else:
                    self.netease_service.consume_music_topic(now)
            except Exception:
                pass
        return topic

    def _is_repeat(self, hint: str) -> bool:
        """A9: 候选 hint 与任一最近已发消息的 jaccard_3gram ≥ 阈值 → 视为复读。"""
        if not hint:
            return False
        for text in self.recent_sent_texts:
            if jaccard_3gram(hint, text) >= self.repeat_jaccard_threshold:
                return True
        return False

    def pick_netease_only(self, now: datetime) -> dict | None:
        """v9 审计 F-4:仅尝试 netease 源(非孤独触发时用)。未注入/无可用 → None。
        选中即确认消费配额(与 pick 的选中后消费语义一致)。
        时段门控(上课/睡眠)由 _netease_music_topic 内部 peek 完成,非活跃时段恒 None。"""
        topic = self._netease_music_topic(now)
        if topic and self.netease_service:
            try:
                if topic.get("type") == "netease_fault":
                    self.netease_service.consume_fault_topic(now)
                else:
                    self.netease_service.consume_music_topic(now)
            except Exception:
                pass
        return topic

    # ── 来源 1：课表/假期/周末 ──────────────────────────────

    def _schedule_topic(self, now: datetime) -> dict | None:
        """基于课表状态的话题。上课中返回 None（不该打扰）。
        #79: schedule_status 异常/课表损坏 → 静默返回 None（不阻塞话题选择）。"""
        try:
            sch = self.state.schedule_status(now)
            if not sch:
                return None

            if sch.get("holiday"):
                return {
                    "type": "schedule",
                    "hint": f"现在是{sch['holiday']}假期，关心哥哥假期过得怎么样",
                    "tone": "casual",
                }
            if sch.get("weekend"):
                return {
                    "type": "schedule",
                    "hint": "今天是周末，关心哥哥周末安排和放松",
                    "tone": "casual",
                }
            if sch.get("makeup_day"):
                return {
                    "type": "schedule",
                    "hint": "虽然是周末但要调休上课，关心哥哥累不累",
                    "tone": "caring",
                }
            if sch.get("in_class"):
                return None
            if sch.get("class_load") == "free":
                return {
                    "type": "schedule",
                    "hint": "哥哥今天没课，问问哥哥今天有什么安排",
                    "tone": "casual",
                }
            if sch.get("remaining_classes", 0) == 0:
                return {
                    "type": "schedule",
                    "hint": "哥哥今天的课上完了，关心上课累不累",
                    "tone": "caring",
                }
            return {
                "type": "schedule",
                "hint": "关心哥哥今天课多不多、累不累",
                "tone": "caring",
            }
        except Exception:
            return None  # #79: 课表数据异常 → 本来源静默跳过

    # ── 来源 2：mem0 记忆 ────────────────────────────────

    def _emotion_tag_kwargs(self) -> dict:
        """B2: 情绪-记忆耦合读侧上下文（kwargs 形式，供记忆检索 **展开）。

        [memory].emotion_tagging=True 且 emotion_tag_weight>0 时返回
        {"emotion_tag": 当前情绪快照, "emotion_tag_weight": 权重}；
        否则返回 {}（恒等——不新增调用参数，兼容不认这两个键的 mock/旧实现）。
        state.config 缺失/非 dict 亦安全回退 {}（默认关闭恒等）。
        """
        cfg = getattr(self.state, "config", None) or {}
        mem_cfg = cfg.get("memory", {}) if isinstance(cfg, dict) else {}
        if not mem_cfg.get("emotion_tagging", False):
            return {}
        try:
            weight = float(mem_cfg.get("emotion_tag_weight", 0.0) or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        if weight <= 0:
            return {}
        return {"emotion_tag": emotion_tag_snapshot(self.state.emotion),
                "emotion_tag_weight": weight}

    def _memory_topic(self) -> dict | None:
        """基于 mem0 随机记忆的话题。数据库不可用时静默跳过。
        v4: 使用 Ebbinghaus 遗忘权重（新记忆更可能被选中）。
             50% 概率使用 search_with_forgetting 做相关性搜索。
        B2: [memory].emotion_tagging=True 时按当前情绪相近加权记忆检索。"""
        if not self.state.memory_bridge.available:
            return None
        emo_kw = self._emotion_tag_kwargs()
        mem = None
        # ── v4: 50% 概率用 Ebbinghaus 搜索（相关性），50% 随机 ──
        if random.random() < 0.5:
            # 用最近触发历史作为搜索上下文
            recent = self.state.cooldown.get_trigger_history()[-3:] if self.state.cooldown.get_trigger_history() else []
            queries = [f"conversation", f"shared experience", f"preference"]
            if recent:
                queries.insert(0, recent[-1])
            for q in queries[:2]:  # 最多试 2 个查询
                results = self.state.memory_bridge.search_with_forgetting(
                    q, limit=3, min_importance=0.3, **emo_kw
                )
                if results:
                    mem = random.choice(results)
                    break
        # fallback: Ebbinghaus 加权随机
        if not mem:
            mem = self.state.memory_bridge.random_memory_with_forgetting(
                min_importance=0.5,
                prefer_categories=["preferences", "entities", "events"],
                **emo_kw,
            )
        if not mem:
            return None
        # C3: l0_abstract 已废弃（mem0 新版不再产出），text 兜底为准。
        abstract = (mem.get("text", "") or mem.get("l0_abstract", "")).strip()
        if not abstract:
            return None
        truncated = abstract[:80]
        # C3: memory_category 死字段清理——优先现成 category 字段，fallback "?"。
        # prefer_categories 匹配逻辑保留（不匹配即不优先，无害）。
        cat = mem.get("category") or mem.get("memory_category") or "?"
        return {
            "type": "memory",
            "hint": f"想起相关记忆：{truncated}，从记忆中自然地找话头",
            "tone": "casual" if cat in ("preferences", "entities") else "caring",
            "data": {"memory": mem},
        }

    # ── 来源 3：季节感知（零依赖） ──────────────────────────

    def _weather_season_topic(self, now: datetime) -> dict:
        """按月份判断季节，生成天气/换季关心提示。"""
        m = now.month
        if 6 <= m <= 8:
            return {
                "type": "weather",
                "hint": "天气很热，提醒哥哥注意防暑、多喝水",
                "tone": "caring",
            }
        if m in (12, 1, 2):
            return {
                "type": "weather",
                "hint": "最近降温了，关心哥哥有没有注意保暖",
                "tone": "caring",
            }
        if m in (3, 4, 5):
            return {
                "type": "weather",
                "hint": "换季时节容易感冒，关心哥哥身体",
                "tone": "caring",
            }
        return {
            "type": "weather",
            "hint": "秋天凉了，提醒哥哥添衣",
            "tone": "caring",
        }

    # ── 来源 4：通用关心（按时间段） ─────────────────────────

    def _general_topic(self, now: datetime) -> dict:
        """按小时选通用关心模板。永远可用。"""
        h = now.hour
        if 6 <= h < 12:
            return {
                "type": "general",
                "hint": "问问哥哥今天上午有什么安排",
                "tone": "casual",
            }
        if 12 <= h < 14:
            return {
                "type": "general",
                "hint": "关心哥哥午饭吃了什么",
                "tone": "casual",
            }
        if 14 <= h < 18:
            return {
                "type": "general",
                "hint": "简单关心哥哥今天过得怎么样",
                "tone": "casual",
            }
        return {
            "type": "general",
            "hint": "关心哥哥晚上在做什么、累不累",
            "tone": "caring",
        }

    # ── 来源 5：节气（零依赖） ──────────────────────────────

    def _solar_terms_topic(self, now: datetime) -> dict | None:
        """检查今天±1天是否接近24节气。"""
        term = self.solar_terms.nearby_term(now.date(), window_days=1)
        if not term:
            return None
        return {
            "type": "solar_term",
            "hint": term["hint"],
            "tone": "casual",
            "data": {"solar_term": {"name": term["name"], "date": term["_match_date"]}},
        }

    # ── 来源 6：纪念日/倒计时 ───────────────────────────────

    def _anniversary_topic(self, now: datetime) -> dict | None:
        """检查当天纪念日或临近倒计时。今天 > 7天内 > 无。
        #79: 纪念日数据异常 → 静默返回 None；name 为 None 时兜底文案"纪念日"。"""
        try:
            today_events = self.state.anniversary_mgr.get_today(now.date())
            if today_events:
                names = "、".join(a.name or "纪念日" for a in today_events)
                return {
                    "type": "anniversary",
                    "hint": f"今天是{names}！提醒哥哥这个特殊日子，表达关心",
                    "tone": "caring",
                    "data": {"anniversaries": [a.__dict__ for a in today_events]},
                }

            upcoming = self.state.anniversary_mgr.get_upcoming(now.date(), days=7)
            if upcoming:
                a, days = upcoming[0]
                return {
                    "type": "anniversary",
                    "hint": f"还有{days}天就是{a.name or '纪念日'}了，问问哥哥有什么打算",
                    "tone": "casual" if days > 3 else "caring",
                    "data": {"anniversary": a.__dict__, "days_until": days},
                }

            return None
        except Exception:
            return None  # #79: 纪念日数据异常 → 本来源静默跳过

    # ── 来源 7：偏好追问（mem0 preferences） ─────────────

    def _preference_followup_topic(self, now: datetime) -> dict | None:
        """查询 mem0 偏好记忆，生成追问话题。
        v4: 使用 Ebbinghaus 遗忘权重。"""
        if not self.state.memory_bridge.available:
            return None
        # B2: 情绪-记忆耦合读侧（emotion_tagging=True 时按当前情绪相近加权）
        emo_kw = self._emotion_tag_kwargs()
        # ── v4: Ebbinghaus 加权搜索 ──
        mem = self.state.memory_bridge.random_memory_with_forgetting(
            min_importance=0.5,
            prefer_categories=["preferences"],
            **emo_kw,
        )
        if not mem:
            return None
        # C3: l0_abstract 已废弃，text 兜底为准。
        text = (mem.get("text", "") or mem.get("l0_abstract", "")).strip()
        if not text:
            return None
        truncated = text[:80]
        return {
            "type": "preference_followup",
            "hint": f"哥哥上次提到{truncated}，追问后来去了吗/试了吗",
            "tone": "casual",
            "data": {"memory": mem},
        }

    # ── 来源 8：网易云音乐（策略层委托） ──

    def _netease_music_topic(self, now: datetime) -> dict | None:
        """v9: 委托 NeteaseService.peek_music_topic(不消费配额,选中后由 pick 确认消费)。
        未注入 → None(不崩溃)。时段门控:上课/睡眠由本方法计算
        (schedule_status + cooldown.quiet_window);门控信息不可得 → fail-closed 不发。"""
        if not self.netease_service or not self.netease_service.enabled:
            return None  # 未注入 / enabled=false → 不探测(A3,与 daemon 播放反证同门控)
        try:
            sch = self.state.schedule_status(now)
            in_class = bool(sch and sch.get("in_class"))
        except Exception:
            return None  # 门控信息不可得 → 不发音乐话题(fail-closed)
        try:
            qs, qe = self.state.cooldown.quiet_window()
            in_quiet = in_quiet_window(now, int(qs), int(qe))
        except Exception:
            return None
        try:
            return self.netease_service.peek_music_topic(now, in_class=in_class,
                                                         in_quiet_window=in_quiet)
        except Exception:
            return None  # 策略层异常 → 静默跳过(不阻塞话题选择)
