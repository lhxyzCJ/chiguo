#!/usr/bin/env python3
"""monitor.stats — stats 视图（#378 纯搬运，零行为变化）。

StatsMixin 由 monitor.base.ChiguoMonitor 组装；self.* helper
（_iter_decisions/_extract_time/_normalize_entry/_read_state/_now/
_alert_day_counts/_rotation_day_counts）运行时经宿主类解析。
Q24 段直接读 self.alerts_path/self.events_path 文件（非调 alerts()）。
"""

import json
import math
import statistics
from collections import Counter
from datetime import timedelta


class StatsMixin:
    """统计引擎视图：stats() + 6 专属 helper（自 chiguo_monitor.py 原样搬运）。"""

    def stats(self, days: int = 7) -> dict:
        """核心统计。一次遍历完成所有聚合。

        Args:
            days: 统计最近 N 天，0 = 全部

        Returns:
            结构化统计字典
        """
        since = None
        if days > 0:
            since = self._now() - timedelta(days=days)
        # B10: 非法决策计数为 stats 窗口粒度——每次统计独立复位，
        # 避免前次遍历（stats/alerts）残留累积污染本次输出。
        self._invalid_decision_count = 0

        # ── D1: 主动消息效果评估（对标 ProactiveEval；[monitor].proactive_eval 默认 False 恒等）──
        proactive_eval = bool(self._monitor_config.get("proactive_eval", False))
        try:
            replied_within_h = float(self._monitor_config.get("replied_within_hours", 24.0))
        except (TypeError, ValueError):
            replied_within_h = 24.0
        if not math.isfinite(replied_within_h):
            replied_within_h = 24.0  # NaN/inf → 回退默认（NaN 比较恒 False 会静默吞掉全部回复）
        send_events: list[tuple] = []  # [(t, trigger)] 按时间序（决策日志顺序）
        recv_times: list = []  # 按时间序的 user-msg 到达时刻

        # ── 累加器 ──
        total_sends = 0
        total_idles = 0
        trigger_counts: Counter = Counter()
        intensity_counts: Counter = Counter()
        hour_counts: Counter = Counter()
        weekday_counts: Counter = Counter()
        daily: dict[str, dict] = {}  # date_str → {sends, idles, idle_reasons}
        idle_reasons: Counter = Counter()

        # 情绪序列（按时间顺序，用于趋势分析）
        emotion_series: dict[str, list[float]] = {
            "loneliness": [], "affection": [], "anxiety": [],
            "energy": [], "tsundere_index": [],
        }

        # 回复追踪
        reply_events: list[dict] = []  # [{time, messages_without_reply}]
        max_unreplied = 0
        layer_counts: Counter = Counter()

        # 时间跨度
        first_time = None
        last_time = None

        # 无法解析时间的条目数（daemon compact ISO 时间混入等，供统计可观测性）
        unparsed_time_count = 0

        # mem0 降级检测
        mem0_ok_count = 0
        mem0_check_count = 0

        # 发送结果统计
        send_success = 0
        send_failed = 0

        for entry in self._iter_decisions(since):
            self._normalize_entry(entry)

            t = self._extract_time(entry)
            if t is None:
                unparsed_time_count += 1
                continue

            # 时间窗口
            if first_time is None or t < first_time:
                first_time = t
            if last_time is None or t > last_time:
                last_time = t

            action = entry.get("action", "?")
            state = entry.get("state", {})

            # 日聚合
            date_key = t.strftime("%Y-%m-%d")
            if date_key not in daily:
                daily[date_key] = {"sends": 0, "idles": 0, "idle_reasons": Counter()}

            if action == "send":
                total_sends += 1
                daily[date_key]["sends"] += 1

                trigger = str(entry.get("trigger", "?") or "?")  # 防损坏行的 dict/list 值 → setdefault 崩溃
                trigger_counts[trigger] += 1
                # D1: 收集发送事件（时间 + trigger）供 proactive_stats 分组统计
                send_events.append((t, trigger))

                intensity = str(entry.get("intensity", "?") or "?")  # 防损坏行的 dict/list 值 → unhashable 崩溃
                intensity_counts[intensity] += 1

                hour_counts[t.hour] += 1
                weekday_counts[t.weekday()] += 1

                # 人格层
                layer = str(state.get("dominant_layer", "?") or "?")  # 防损坏行的 dict/list 值 → unhashable 崩溃
                layer_counts[layer] += 1

                # 回复追踪：messages_without_reply
                cooldown = state.get("cooldown", {})
                mwr = cooldown.get("messages_without_reply", 0)
                if isinstance(mwr, (int, float)):
                    max_unreplied = max(max_unreplied, int(mwr))

                # 从 reply_latencies 收集已知延迟（state 快照不直接包含，但从 cooldown 推断）
                silent_h = cooldown.get("silent_hours", 0)
                reply_events.append({
                    "time": t.isoformat(),
                    "messages_without_reply": mwr if isinstance(mwr, (int, float)) else None,
                    "silent_hours": silent_h if isinstance(silent_h, (int, float)) else 0,
                })

                # mem0 可用性：有 memory 触发 → mem0 正常
                if trigger == "memory":
                    # 检查是否有 mem0_memory（situation 含「突然想起」或 context 含 mem0）
                    ctx = entry.get("context")
                    if not isinstance(ctx, dict):
                        ctx = {}
                    sit = ctx.get("situation") or ""
                    if "突然想起" in sit or "mem0" in str(ctx).lower():
                        mem0_ok_count += 1
                    mem0_check_count += 1

            elif action == "idle":
                total_idles += 1
                daily[date_key]["idles"] += 1
                reason = str(entry.get("reason", "?") or "?")  # 防损坏行的 dict/list 值 → unhashable 崩溃
                idle_reasons[reason] += 1
                daily[date_key]["idle_reasons"][reason] += 1

            elif action == "send_result":
                if entry.get("status") == "success":
                    send_success += 1
                # R2: --user-msg 幻影发送退款（phantom_send_reply_path）不计入 send_failed——
                # 该记录是回复链内部回滚未送达的决策 booking，非真实发送失败。
                elif entry.get("status") == "failed" and entry.get("error") != "phantom_send_reply_path":
                    send_failed += 1

            elif action == "recv":
                # D1: 用户消息到达时刻（主动消息效果评估的"已回复"数据源）
                recv_times.append(t)

            # 情绪时间序列
            emo = state.get("emotion", {})
            for key in emotion_series:
                val = emo.get(key)
                if isinstance(val, (int, float)):
                    emotion_series[key].append(float(val))

        # ── 计算派生指标 ──

        # 时间跨度
        span_days = 0.0
        if first_time and last_time:
            # 下限 1 天：不足 1 天的窗口按 1 天计，避免 0.01 放大日均发送量失真
            span_days = max(1.0, (last_time - first_time).total_seconds() / 86400)
        sends_per_day = total_sends / span_days if span_days > 0 else 0.0

        # 情绪趋势
        emotion_trends = {}
        emotion_stats = {}
        for key in emotion_series:
            vals = emotion_series[key]
            if len(vals) >= 4:
                emotion_trends[key] = self._trend(vals)
                emotion_stats[key] = {
                    "min": round(min(vals), 1),
                    "max": round(max(vals), 1),
                    "avg": round(statistics.mean(vals), 1),
                    "current": round(vals[-1], 1),
                    "count": len(vals),
                }
            elif len(vals) > 0:
                emotion_trends[key] = "stable"
                emotion_stats[key] = {
                    "min": round(min(vals), 1),
                    "max": round(max(vals), 1),
                    "avg": round(statistics.mean(vals), 1),
                    "current": round(vals[-1], 1),
                    "count": len(vals),
                }
            else:
                emotion_trends[key] = "no_data"
                emotion_stats[key] = None

        # 回复率估算（用 silent_hours 变化推断：silent_hours 骤降 = 有回复）
        # 口径定义（与 alerts() B5 一致）：
        #   分子 = 相邻 send 中 messages_without_reply 下降的对数（检测到回复）
        #   分母 = 窗口内等待过回复的 send 数（mwr > 0，mwr=0 的 send 无回复可收，不参与）
        reply_rate = None
        avg_latency = None
        median_latency = None
        if reply_events and len(reply_events) >= 2:
            replied = 0
            latencies = []
            for i in range(1, len(reply_events)):
                prev = reply_events[i - 1]
                curr = reply_events[i]
                # messages_without_reply 下降 → 哥哥回复了
                # 口径（与 alerts() B5 一致）：双方均为数值才比较；
                # None/非数值视为未知，不计为回复变化（防止 prev=3, curr=None→0 误算成回复）
                if (isinstance(curr["messages_without_reply"], (int, float))
                        and isinstance(prev["messages_without_reply"], (int, float))
                        and curr["messages_without_reply"] < prev["messages_without_reply"]):
                    replied += 1
                    latencies.append(curr.get("silent_hours", 0))
            total_send_events = sum(1 for e in reply_events
                                    if isinstance(e["messages_without_reply"], (int, float))
                                    and e["messages_without_reply"] > 0)
            if total_send_events > 0:
                reply_rate = round(replied / total_send_events, 3)
            if latencies:
                avg_latency = statistics.mean(latencies)
                median_latency = statistics.median(latencies)

        # 当前状态
        state_data = self._read_state()
        current_emotion = None
        last_tick = None
        if state_data:
            current_emotion = state_data.get("emotion")
            last_tick = state_data.get("last_tick")

        # 日计数序列（按日期排序）
        daily_list = [
            {"date": d, "sends": v["sends"], "idles": v["idles"],
             "top_idle_reason": v["idle_reasons"].most_common(1)[0][0] if v["idle_reasons"] else None}
            for d, v in sorted(daily.items())
        ]

        mem0_likely = mem0_ok_count > 0

        # ── Q24 (#275): 事件时序（复用 proactive_stats 的每日计数口径）──
        # 告警按 chiguo_alerts.json 各告警的 first_seen 归日计数；轮转等事件
        # 按 chiguo_events.jsonl 的 at 字段归日计数。与统计窗口(days)对齐。
        events = {}
        alert_day_counts = self._alert_day_counts(days)
        if alert_day_counts:
            events["alerts_by_day"] = alert_day_counts
        rot_day_counts = self._rotation_day_counts(days)
        if rot_day_counts:
            events["rotations_by_day"] = rot_day_counts

        # ── D1: 主动消息效果评估（按 trigger 分组：发送后 replied_within_h 内
        # 收到首条 user-msg 视为已回复；双指针流式，一次遍历 O(n)）──
        # 语义：一条 user-msg 至多算作一条主动消息的回复——命中窗口即消费
        # （recv_ptr 前进），后续 send 不再重复计同一条回复（防串计）。
        proactive_stats = None
        if proactive_eval:
            per_trigger: dict[str, dict] = {}
            recv_ptr = 0
            n_sends = len(send_events)
            for idx, (s_time, trig) in enumerate(send_events):  # 决策日志时间序
                bucket = per_trigger.setdefault(trig, {"sent": 0, "replied": 0})
                bucket["sent"] += 1
                # 窗口上界收窄到下一条 send 时刻：离下一条更近的迟到回复不得串计给本条，
                # 避免贪心 FIFO 把其实回复下一条 send 的 recv 错记给上一条。
                if idx + 1 < n_sends:
                    upper = min(send_events[idx + 1][0], s_time + timedelta(hours=replied_within_h))
                else:
                    upper = s_time + timedelta(hours=replied_within_h)
                while recv_ptr < len(recv_times) and recv_times[recv_ptr] <= s_time:
                    recv_ptr += 1  # 跳过发送时刻之前的 user-msg（非本消息的回复）
                if recv_ptr < len(recv_times) and recv_times[recv_ptr] <= upper:
                    bucket["replied"] += 1
                    recv_ptr += 1  # 消费该回复：不重复计给更晚的 send
            for trig, b in per_trigger.items():
                b["reply_rate"] = round(b["replied"] / b["sent"], 3) if b["sent"] else 0.0
            overall_sent = sum(b["sent"] for b in per_trigger.values())
            overall_replied = sum(b["replied"] for b in per_trigger.values())
            proactive_stats = dict(per_trigger)
            proactive_stats["overall"] = {
                "sent": overall_sent,
                "replied": overall_replied,
                "reply_rate": (round(overall_replied / overall_sent, 3)
                               if overall_sent else 0.0),
            }

        out = {
            "period": {
                "days": days if days > 0 else "all",
                "from": first_time.isoformat() if first_time else None,
                "to": last_time.isoformat() if last_time else None,
                "span_days": round(span_days, 1),
                "total_entries": total_sends + total_idles,  # 口径：send + idle 决策条数（不含 recv/send_result）
                "unparsed_time_count": unparsed_time_count,
                # B10: 窗口内被决策 schema 判为非法的记录数（decision_schema.validate 非空）
                "invalid_decision_count": self._invalid_decision_count,
            },
            "activity": {
                "total_sends": total_sends,
                "total_idles": total_idles,
                "sends_per_day": round(sends_per_day, 2),
                "by_trigger": dict(trigger_counts.most_common()),
                "by_intensity": dict(intensity_counts),
                "by_hour": {str(h): hour_counts[h] for h in sorted(hour_counts)},
                "by_weekday": {
                    self._weekday_name(d): weekday_counts.get(d, 0)
                    for d in range(7)
                },
                "by_layer": dict(layer_counts),
                "idle_reasons": dict(idle_reasons.most_common()),
                "daily_counts": daily_list,
            },
            "replies": {
                "max_unreplied_streak": max_unreplied,
                "reply_rate": reply_rate,
                "avg_reply_latency_h": round(avg_latency, 1) if avg_latency else None,
                "median_reply_latency_h": round(median_latency, 1) if median_latency else None,
                "total_send_events_tracked": len(reply_events),
            },
            "emotions": {
                "current": current_emotion,
                "trends": emotion_trends,
                "stats": emotion_stats,
            },
            "mem0": {
                "memory_trigger_count": mem0_check_count,
                "mem0_ok_estimate": mem0_ok_count,
                "likely_available": mem0_likely,
            } if mem0_check_count > 0 else None,
            "send_result": {
                "success": send_success,
                "failed": send_failed,
            },
        }
        # D1: 默认关闭恒等——proactive_eval=False 时不新增输出键
        if proactive_stats is not None:
            out["proactive_stats"] = proactive_stats
        # Q24: 事件时序——仅在存在事件数据时新增输出键（保持空闲恒等）
        if events:
            out["events"] = events
        return out

    @staticmethod
    def _weekday_name(d: int) -> str:
        return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]

    @staticmethod
    def _trend(values: list[float], threshold_ratio: float = 0.03) -> str:
        """首半 vs 后半均值比较 → rising/stable/falling"""
        n = len(values)
        if n < 4:
            return "stable"
        half = n // 2
        first_avg = statistics.mean(values[:half])
        second_avg = statistics.mean(values[half:])
        if first_avg == 0:
            return "stable"
        change = (second_avg - first_avg) / max(abs(first_avg), 1)
        if change > threshold_ratio:
            return "rising"
        elif change < -threshold_ratio:
            return "falling"
        return "stable"

    @staticmethod
    def _event_day_counts(path) -> dict[str, int]:
        """从 JSONL 事件文件（chiguo_events.jsonl）按 at 日期计数。
        文件缺失/损坏行/缺 at 字段 → 跳过，不崩溃。"""
        counts: dict[str, int] = {}
        if not path.exists():
            return counts
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        rec = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    at = rec.get("at")
                    if not at:
                        continue
                    day = at[:10]  # ISO YYYY-MM-DD
                    if len(day) != 10:
                        continue
                    counts[day] = counts.get(day, 0) + 1
        except OSError:
            pass
        return counts

    def _filter_days(self, counts: dict[str, int], days: int) -> dict[str, int]:
        """把按日计数裁剪到最近 days 天（days<=0 表示全部）。"""
        if days is None or days <= 0:
            return dict(counts)
        cutoff = (self._now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return {d: c for d, c in counts.items() if d >= cutoff}

    def _alert_day_counts(self, days: int) -> dict[str, int]:
        """统计每日告警数：按 chiguo_alerts.json 各告警 first_seen 归日。
        alerts_path 缺失/损坏 → 空 dict。与 --stats days 窗口对齐。"""
        counts: dict[str, int] = {}
        if not self.alerts_path.exists():
            return {}
        try:
            data = json.loads(self.alerts_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}
        alerts = data.get("alerts", {}) if isinstance(data, dict) else {}
        if not isinstance(alerts, dict):
            return {}
        for alert in alerts.values():
            if not isinstance(alert, dict):
                continue
            first_seen = alert.get("first_seen") or alert.get("detected_at")
            if not first_seen or not isinstance(first_seen, str) or len(first_seen) < 10:
                continue
            day = first_seen[:10]
            counts[day] = counts.get(day, 0) + 1
        return self._filter_days(counts, days)

    def _rotation_day_counts(self, days: int) -> dict[str, int]:
        """统计每日轮转/事件数：按 chiguo_events.jsonl 的 at 字段归日。
        与 --stats days 窗口对齐。"""
        return self._filter_days(self._event_day_counts(self.events_path), days)
