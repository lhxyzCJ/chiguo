#!/usr/bin/env python3
"""monitor.alerts — alerts 视图 + AlertManager + collect_new_alerts_to_push
（#378 纯搬运，零行为变化；B3-B6 探针只搬运不拆子函数）。

AlertsMixin 由 monitor.base.ChiguoMonitor 组装；self.* helper
（_iter_decisions/_extract_time/_normalize_entry/_read_state/
_read_break_state/_now）运行时经宿主类解析。
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from chiguo_atomic import atomic_write
from chiguo_time import CST

from .helpers import _num


class AlertsMixin:
    """异常检测视图：alerts()（自 chiguo_monitor.py 原样搬运）。"""

    def alerts(self) -> list[dict]:
        """扫描日志 + 状态，返回所有异常告警。按严重度排序。"""
        results: list[dict] = []
        now = self._now()

        # ── A. 崩溃间隙（daemon 停跑 > 6h） ──
        state = self._read_state()
        last_tick_str = state.get("last_tick")
        if last_tick_str:
            try:
                last_tick = datetime.fromisoformat(last_tick_str)
                if last_tick.tzinfo is None:
                    last_tick = last_tick.replace(tzinfo=CST)  # naive → 视为 CST
                gap_h = (now - last_tick).total_seconds() / 3600
                if gap_h > 6:
                    results.append({
                        "severity": "critical",
                        "type": "crash_gap",
                        "message": f"daemon 上次 tick 在 {gap_h:.1f} 小时前（>6h），可能已停止运行",
                        "last_tick": last_tick_str,
                        "gap_hours": round(gap_h, 1),
                    })
                elif gap_h > 3:
                    results.append({
                        "severity": "info",
                        "type": "slow_tick",
                        "message": f"daemon 上次 tick 在 {gap_h:.1f} 小时前（>3h），可能 cron 频率过低",
                        "last_tick": last_tick_str,
                        "gap_hours": round(gap_h, 1),
                    })
            except ValueError, TypeError:
                pass
        else:
            results.append({
                "severity": "critical",
                "type": "no_state",
                "message": "chiguo_state.json 不存在或无 last_tick，daemon 可能从未运行",
            })

        # ── B. 日志扫描异常 ──
        # 只看最近 14 天
        since = now - timedelta(days=14)
        recent_entries = list(self._iter_decisions(since))
        # 预计算并缓存时间戳，避免 B1/B2/B3/B6 重复调用 _extract_time
        for e in recent_entries:
            self._normalize_entry(e)  # 与 stats() 相同的脏数据归一化（state:null 等）
            e["_cached_ts"] = self._extract_time(e)


        sends = [e for e in recent_entries if e.get("action") == "send"]
        idles = [e for e in recent_entries if e.get("action") == "idle"]

        # B1. 连续无回复
        max_mwr = 0
        mwr_times: list[tuple[int, str]] = []
        for e in sends:
            mwr = e.get("state", {}).get("cooldown", {}).get("messages_without_reply", 0)
            if isinstance(mwr, (int, float)) and mwr > max_mwr:
                max_mwr = int(mwr)
            if isinstance(mwr, (int, float)):
                ts = e.get("_cached_ts")
                mwr_times.append((int(mwr), ts.isoformat() if ts else "?"))

        if max_mwr >= 5:
            # 找到最近一次达到5的条目
            last_high = None
            for m, t in reversed(mwr_times):
                if m >= 5:
                    last_high = t
                    break
            results.append({
                "severity": "warn",
                "type": "consecutive_no_reply",
                "message": f"哥哥连续 {max_mwr} 条消息未回复",
                "max_unreplied": max_mwr,
                "last_at": last_high,
            })

        # B2. 情绪极端卡住
        if sends:
            recent_sends_24h = []
            for e in sends:
                t = e["_cached_ts"] if "_cached_ts" in e else self._extract_time(e)
                if t and (now - t).total_seconds() < 86400:
                    recent_sends_24h.append(e)

            if recent_sends_24h:
                high_lo = sum(1 for e in recent_sends_24h
                              if _num(e.get("state", {}).get("emotion", {}).get("loneliness", 0)) > 90)
                high_anx = sum(1 for e in recent_sends_24h
                               if _num(e.get("state", {}).get("emotion", {}).get("anxiety", 0)) > 90)
                total_24h = len(recent_sends_24h)
                if total_24h >= 3 and high_lo >= total_24h * 0.8:
                    results.append({
                        "severity": "warn",
                        "type": "emotion_stuck_high",
                        "message": f"过去24h内 {high_lo}/{total_24h} 次发送时孤独值 > 90，情绪持续极端",
                        "emotion": "loneliness",
                    })
                if total_24h >= 3 and high_anx >= total_24h * 0.8:
                    results.append({
                        "severity": "warn",
                        "type": "emotion_stuck_high",
                        "message": f"过去24h内 {high_anx}/{total_24h} 次发送时不安值 > 90，情绪持续极端",
                        "emotion": "anxiety",
                    })

            # B3. 低元气卡住（复用 B2 的 recent_sends_24h，避免重复 _extract_time 调用）
            if recent_sends_24h:
                low_energy = sum(1 for e in recent_sends_24h
                                 if _num(e.get("state", {}).get("emotion", {}).get("energy", 0)) < 15)
                if len(recent_sends_24h) >= 6 and low_energy >= len(recent_sends_24h) * 0.7:
                    results.append({
                        "severity": "info",
                        "type": "emotion_stuck_low",
                        "message": f"过去24h内 {low_energy}/{len(recent_sends_24h)} 次发送时元气 < 15，可能过度发送",
                        "emotion": "energy",
                    })

        # B4. 频繁崩溃触发（kernel 层占比 > 40%）
        if len(sends) >= 10:
            kernel_count = sum(1 for e in sends
                               if e.get("state", {}).get("dominant_layer") == "kernel")
            kernel_ratio = kernel_count / len(sends)
            if kernel_ratio > 0.4:
                results.append({
                    "severity": "warn",
                    "type": "frequent_crash",
                    "message": f"过去14天 {kernel_count}/{len(sends)}（{kernel_ratio:.0%}）次发送处于 kernel 崩溃层",
                    "kernel_ratio": round(kernel_ratio, 2),
                })

        # B5. 低回复率
        # 口径与 stats() 一致（见 stats() 回复率估算注释）：
        #   分子 = 相邻 send 中 messages_without_reply 下降的对数（检测到回复）
        #   分母 = 等待过回复的 send 数（mwr > 0），而非全部 send 数
        if len(sends) >= 5:
            replied = 0
            for i in range(1, len(sends)):
                prev_mwr = sends[i - 1].get("state", {}).get("cooldown", {}).get("messages_without_reply", 0)
                curr_mwr = sends[i].get("state", {}).get("cooldown", {}).get("messages_without_reply", 0)
                if isinstance(curr_mwr, (int, float)) and isinstance(prev_mwr, (int, float)):
                    if curr_mwr < prev_mwr:
                        replied += 1
            tracked = sum(1 for e in sends
                          if isinstance(e["state"]["cooldown"].get("messages_without_reply"), (int, float))
                          and e["state"]["cooldown"].get("messages_without_reply", 0) > 0)
            if tracked > 0:
                rate = replied / tracked
                if rate < 0.3:
                    results.append({
                        "severity": "warn",
                        "type": "low_reply_rate",
                        "message": f"过去14天回复率仅 {rate:.0%}（{replied}/{tracked}），主人可能逐渐疏远",
                        "reply_rate": round(rate, 2),
                    })

        # B6. 情绪快速攀升（24h 内 loneliness 涨 > 40）
        emotion_vals_24h = []
        for e in recent_entries:
            ts = e["_cached_ts"] if "_cached_ts" in e else self._extract_time(e)
            if ts and (now - ts).total_seconds() < 86400:
                lo = e.get("state", {}).get("emotion", {}).get("loneliness", 0)
                if isinstance(lo, (int, float)):
                    emotion_vals_24h.append((ts, float(lo)))
        if len(emotion_vals_24h) >= 4:
            emotion_vals_24h.sort(key=lambda x: x[0])
            first_lo = emotion_vals_24h[0][1]
            last_lo = emotion_vals_24h[-1][1]
            delta = last_lo - first_lo
            if delta > 40:
                results.append({
                    "severity": "info",
                    "type": "rapid_escalation",
                    "message": f"过去24h孤独值暴涨 {delta:.0f}（{first_lo:.0f} → {last_lo:.0f}），主人可能异常沉默",
                    "delta": round(delta, 1),
                })

        # ── C. mem0 降级检测 ──
        memory_triggers = sum(1 for e in sends if e.get("trigger") == "memory")
        if len(sends) >= 10 and memory_triggers == 0:
            results.append({
                "severity": "info",
                "type": "mem0_possible_degradation",
                "message": f"过去14天 {len(sends)} 次发送中无 memory 触发，mem0 可能不可用",
            })

        # ── D. 假期状态异常 ──
        break_data = self._read_break_state()
        if break_data.get("manual_override"):
            since_str = break_data.get("since", "?")
            results.append({
                "severity": "info",
                "type": "manual_break_active",
                "message": f"假期模式手动无限期开启中（since {since_str}）。如已开学请执行 --break off",
            })

        # 按严重度排序：critical > warn > info
        severity_order = {"critical": 0, "warn": 1, "info": 2}
        results.sort(key=lambda a: severity_order.get(a["severity"], 3))
        return results


# ═══════════════════════════════════════════════════════════
# AlertManager — 告警生命周期管理
# ═══════════════════════════════════════════════════════════

class AlertManager:
    """告警持久化：active → acknowledged → resolved 生命周期。
    状态存储在 chiguo_alerts.json，原子写入。
    按 alert type 去重 —— 同类型重复触发只更新 last_seen。
    """

    def __init__(self, state_path: str = "chiguo_alerts.json"):
        self.state_path = Path(state_path)
        self._alerts: dict[str, dict] = {}
        self._load()

    def _load(self):
        """加载告警状态文件。缺失/损坏/形状错误 → 空字典。"""
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    self._alerts = {}  # 顶层非 dict（如列表/字符串）→ 回退空
                    return
                alerts = data.get("alerts", {})
                self._alerts = alerts if isinstance(alerts, dict) else {}
        except json.JSONDecodeError, OSError, UnicodeDecodeError:
            self._alerts = {}

    def _save(self):
        """原子写入：tmp → os.replace（Q23: 收敛至共享 chiguo_atomic）。"""
        try:
            data = json.dumps({
                "_version": 1,
                "alerts": self._alerts,
            }, ensure_ascii=False, indent=2)
            atomic_write(self.state_path, data, mode=0o600)
        except OSError as e:
            print(f"[monitor] AlertManager._save 写入失败: {e}", file=sys.stderr)

    @staticmethod
    def _make_alert_id(alert_type: str) -> str:
        """从类型生成稳定ID（同一类型始终同ID，方便去重）。"""
        return f"alert_{alert_type}"

    def ingest(self, fresh_alerts: list[dict]) -> list[dict]:
        """摄入 monitor.alerts() 结果，与持久化状态合并。

        规则：
        - 新类型 → 创建 alert, status='active', count=1
        - 已有类型仍触发 → 更新 last_seen, count++, acknowledged 保持不变
        - 已有类型未触发 → 标记 status='resolved'
        """
        now = datetime.now(CST)
        fresh_types = set()

        for fa in fresh_alerts:
            alert_type = fa.get("type", "unknown")
            fresh_types.add(alert_type)
            alert_id = self._make_alert_id(alert_type)

            if alert_id in self._alerts:
                existing = self._alerts[alert_id]
                existing["last_seen"] = now.isoformat()
                prev_count = existing.get("count")
                # count 非数值（损坏文件）→ 重置为 1 再自增，避免 TypeError
                if not isinstance(prev_count, (int, float)) or isinstance(prev_count, bool):
                    prev_count = 1
                existing["count"] = prev_count + 1
                # 如果之前已 resolved，重新触发
                if existing.get("status") == "resolved":
                    existing["status"] = "active"
                    existing["resolved_at"] = None
                    existing["acknowledged_at"] = None  # 重置确认状态
                    # first_seen 保持不变（保留首次触发时间）
                existing["message"] = fa.get("message", existing.get("message", ""))
                existing["severity"] = fa.get("severity", existing.get("severity", "info"))
            else:
                self._alerts[alert_id] = {
                    "alert_id": alert_id,
                    "type": alert_type,
                    "severity": fa.get("severity", "info"),
                    "message": fa.get("message", ""),
                    "first_seen": now.isoformat(),
                    "last_seen": now.isoformat(),
                    "count": 1,
                    "status": "active",
                    "acknowledged_at": None,
                    "resolved_at": None,
                }

        # 标记已消失的告警为 resolved
        for alert_id, alert in self._alerts.items():
            alert_type = alert.get("type", "")
            if alert_type not in fresh_types and alert.get("status") != "resolved":
                alert["status"] = "resolved"
                alert["resolved_at"] = now.isoformat()

        self._save()
        return self.list_active()

    def acknowledge(self, alert_id: str) -> bool:
        """确认告警。"""
        if alert_id in self._alerts:
            self._alerts[alert_id]["status"] = "acknowledged"
            self._alerts[alert_id]["acknowledged_at"] = datetime.now(CST).isoformat()
            self._save()
            return True
        return False

    def list_active(self) -> list[dict]:
        """返回 active + acknowledged 告警（不含 resolved）。"""
        return [a for a in self._alerts.values()
                if a.get("status") in ("active", "acknowledged")]

    def list_all(self) -> list[dict]:
        """返回所有告警（含 resolved）。"""
        return list(self._alerts.values())


# ═══════════════════════════════════════════════════════════
# Q24 (#275): 告警 cron 化——微信推送入口
# ═══════════════════════════════════════════════════════════

def collect_new_alerts_to_push(monitor: "AlertsMixin",
                               alert_manager: AlertManager,
                               now: datetime | None = None) -> list[dict]:
    """生成告警 → 摄入 AlertManager → 返回「本次新增、需经微信推送」的告警。

    cron 侧（--alerts-push / scripts/alert-cron.sh）复用此入口，行为：
    - 调 monitor.alerts() 检出当前异常
    - 调 alert_manager.ingest() 持久化（active→acknowledged→resolved 生命周期）
    - 仅推送「新增活跃」的 critical/warn 告警（推送前不存在于 active 集合）；
      已在活跃态、cron 每次重复运行不再重推（按 alert type 天然去重）。
    - 返回被推送的告警列表（empty = 本次无新告警需打扰）。

    返回告警上附加的 `pushed`/`pushed_at` 为 **CLI 输出专用元数据，不持久化**——
    `ingest()` 在附加前已 `_save()` 落盘 chiguo_alerts.json，且 cron 每次全新进程
    也不会回写；它们仅供 --alerts-push 的 stdout JSON 展示，去重不依赖它们。

    Args:
        monitor: 已构造的 ChiguoMonitor（alerts() 数据源）。
        alert_manager: 已构造的 AlertManager（持久化与去重）。
        now: 可选固定时间（测试用）；缺省 datetime.now(CST)。
    """
    before = {a["alert_id"] for a in alert_manager.list_active()}
    fresh_alerts = monitor.alerts()
    active = alert_manager.ingest(fresh_alerts)  # active + acknowledged
    pushed: list[dict] = []
    _now = now or datetime.now(CST)
    for alert in active:
        alert_id = alert.get("alert_id")
        if alert_id in before:
            continue  # 已活跃，非本次新增，不重复推
        severity = alert.get("severity")
        if severity not in ("critical", "warn"):
            continue  # info 级不打扰
        if alert.get("status") != "active":
            continue  # 新增即非 active（acknowledged/resolved）→ 不推
        # pushed/pushed_at 仅 CLI 输出用元数据，不持久化（ingest 已 _save；cron 全新建进程不回写）
        alert["pushed"] = True
        alert["pushed_at"] = _now.isoformat()
        pushed.append(alert)
    return pushed
