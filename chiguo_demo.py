#!/usr/bin/env python3
# ============================================================
# chiguo_demo.py — 迟菓主动消息系统 交互式演示 v2
# 数学驱动：Sigmoid + 半衰期 + 动态 λ
#
# ⚠️ 演示模式：仅展示触发决策与情境组合（MessageComposer），
# 不发送/不落盘决策日志，与生产行为不同
# ============================================================

import os
import tomllib
from datetime import datetime, timedelta, timezone

os.chdir(os.path.dirname(os.path.abspath(__file__)))
CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState
from chiguo_trigger import evaluate_triggers
from chiguo_composer import MessageComposer
from chiguo_math import sigmoid


class Color:
    R = "\033[0m"; B = "\033[1m"; D = "\033[2m"
    RED = "\033[31m"; GRN = "\033[32m"; YEL = "\033[33m"
    BLU = "\033[34m"; MAG = "\033[35m"; CYN = "\033[36m"

    @staticmethod
    def bar(v, w=20):
        f = int(v / 100 * w)
        if v > 70: c = Color.RED
        elif v > 40: c = Color.YEL
        else: c = Color.BLU
        return f"{c}{'█'*f}{Color.D}{'░'*(w-f)}{Color.R}"


class Demo:
    def __init__(self):
        self.sim_now = datetime.now(CST)
        with open("chiguo_proactive.toml", "rb") as f:
            self.cfg = tomllib.load(f)
        self.state = ChiguoState(self.cfg)
        self.composer = MessageComposer(self.state, self.cfg.get("composer", {}))
        self.sent: list[dict] = []

    def tick(self, mins: float):
        self.sim_now += timedelta(minutes=mins)
        self.state.tick(mins / 60, self.sim_now)

        if not self.state.can_send(self.sim_now):
            return

        trigger = evaluate_triggers(self.state, self.sim_now)
        if not trigger:
            return

        combo = self.composer.select_combo(trigger.type, self.sim_now)
        silent_h = self.state.cooldown.silent_hours(self.sim_now)
        situation = self.composer.compose_situation(combo, None, silent_h)
        self.sent.append({"time": self.sim_now.isoformat(), "message": situation})
        self.state.on_character_message(self.sim_now, trigger.type)
        if trigger.type == "morning":
            self.state.cooldown.morning_sent = True
        elif trigger.type == "night":
            self.state.cooldown.night_sent = True

    def user_msg(self, text: str):
        print(f"\n{Color.GRN}📩 主人: {text}{Color.R}")
        self.state.on_user_message(self.sim_now, len(text))

    def render(self):
        s = self.state.snapshot(self.sim_now)
        e = s["emotion"]
        c = s["cooldown"]
        lam = s["poisson_lambda"]
        layer_icons = {"shell": "🌞外壳", "middle": "💢中层", "kernel": "💔内核"}
        layer = s["dominant_layer"]

        print(f"\n{Color.B}{Color.MAG}╔══════════════════════════════════════════════╗{Color.R}")
        print(f"{Color.B}{Color.MAG}║{Color.R}  {Color.B}迟菓{Color.R} — {layer_icons.get(layer, layer)}")

        # 情绪条 + 触发概率
        lo = e["loneliness"]
        anx = e["anxiety"]
        trig_lo = sigmoid(lo, self.cfg["sigmoid"]["loneliness_low_mid"], self.cfg["sigmoid"]["loneliness_low_k"])
        trig_mid = sigmoid(lo, self.cfg["sigmoid"]["loneliness_mid_mid"], self.cfg["sigmoid"]["loneliness_mid_k"])
        trig_hi = sigmoid(lo, self.cfg["sigmoid"]["loneliness_high_mid"], self.cfg["sigmoid"]["loneliness_high_k"])

        print(f"{Color.B}{Color.MAG}║{Color.R} {Color.D}孤独{Color.R}  {Color.bar(lo)} {lo:5.0f}  P(试探)={trig_lo:.2f} P(嘴硬)={trig_mid:.2f} P(崩溃)={trig_hi:.2f}")
        print(f"{Color.B}{Color.MAG}║{Color.R} {Color.D}好感{Color.R}  {Color.bar(e['affection'], 20)} {e['affection']:5.0f}")
        print(f"{Color.B}{Color.MAG}║{Color.R} {Color.D}不安{Color.R}  {Color.bar(anx, 20)} {anx:5.0f}  P(不安触发)={sigmoid(anx, self.cfg['sigmoid']['anxiety_mid'], self.cfg['sigmoid']['anxiety_k']):.2f}")
        print(f"{Color.B}{Color.MAG}║{Color.R} {Color.D}元气{Color.R}  {Color.bar(e['energy'], 20)} {e['energy']:5.0f}")
        print(f"{Color.B}{Color.MAG}║{Color.R} {Color.D}傲娇{Color.R}  {Color.bar(e['tsundere_index'], 20)} {e['tsundere_index']:5.0f}")

        print(f"{Color.B}{Color.MAG}╠══════════════════════════════════════════════╣{Color.R}")
        # 假期模式
        sch = s.get("schedule")
        if sch and sch.get("on_break"):
            br = sch.get("break_reason", "未知")
            breaks = sch.get("breaks", [])
            active = [b for b in breaks if b.get("active")]
            detail = ""
            if active:
                detail = " | " + ", ".join(b.get("note", b["start"]) for b in active)
            print(f"{Color.B}{Color.MAG}║{Color.R} {Color.YEL}🏖 假期模式: {br}{detail}{Color.R}")
        # 节假日行（优先级最高）
        hol = s.get("holiday", {})
        if hol.get("is_holiday"):
            print(f"{Color.B}{Color.MAG}║{Color.R} {Color.GRN}🎉 {hol['name']}假期！主人放假{Color.R}")
        elif hol.get("is_makeup_workday"):
            print(f"{Color.B}{Color.MAG}║{Color.R} {Color.YEL}📅 调休日，今天要上课{Color.R}")
        elif hol.get("is_weekend"):
            print(f"{Color.B}{Color.MAG}║{Color.R} {Color.GRN}🏖 周末{Color.R}")
        # 课表行
        if sch and not hol.get("is_holiday") and not (hol.get("is_weekend") and not hol.get("is_makeup_workday")):
            if sch["in_class"]:
                print(f"{Color.B}{Color.MAG}║{Color.R} {Color.RED}📚 上课中: {sch['current_course']}{Color.R}")
            elif sch.get("makeup_day"):
                print(f"{Color.B}{Color.MAG}║{Color.R} {Color.YEL}📅 调休上课日{Color.R}")
            elif sch["class_load"] == "free":
                print(f"{Color.B}{Color.MAG}║{Color.R} {Color.GRN}🆓 今天没课{Color.R}")
            elif sch.get("remaining_classes", 0) == 0:
                print(f"{Color.B}{Color.MAG}║{Color.R} {Color.GRN}✅ 课上完了{Color.R}")
            else:
                print(f"{Color.B}{Color.MAG}║{Color.R} 课业:{sch['class_load']} 剩{sch['remaining_classes']}节 avail={s['availability']}")
        print(f"{Color.B}{Color.MAG}║{Color.R} Poisson λ={lam:.4f}/h | 今日{c['messages_today']}条 | 沉默{c['silent_hours']:.1f}h | {'可发' if c['can_send'] else '禁发'}")
        # 记忆系统
        mem_stats = self.state.memory_bridge.stats()
        print(f"{Color.B}{Color.MAG}║{Color.R} 🧠 mem0记忆: {mem_stats['total_memories']}总/{mem_stats['user_relevant_count']}相关")
        print(f"{Color.B}{Color.MAG}║{Color.R} 上次消息: {c['minutes_since_last']:.0f}min前 | {s['time']}")

        recent = self.sent[-3:]
        if recent:
            print(f"{Color.B}{Color.MAG}╠══════════════════════════════════════════════╣{Color.R}")
            for r in recent:
                print(f"{Color.B}{Color.MAG}║{Color.R} [{r['time']}] {r['message'][:45]}")
        print(f"{Color.B}{Color.MAG}╚══════════════════════════════════════════════╝{Color.R}")

    def run(self):
        from chiguo_version import VERSION
        print(f"\n🌸 迟菓 主动消息系统 v{VERSION}")
        print(f"   数学: Sigmoid + 半衰期 + 动态 λ")
        print(f"{Color.YEL}⚠️ 演示模式：仅展示触发决策与情境组合，不发送，与生产行为不同{Color.R}")
        self.render()

        while True:
            try:
                raw = input(f"\n{Color.CYN}⏱ [{self.sim_now.strftime('%m/%d %H:%M')}] > {Color.R}").strip()
            except (EOFError, KeyboardInterrupt):
                print(); break

            if not raw:
                self.tick(30); self.render(); continue

            p = raw.split(maxsplit=1)
            cmd, arg = p[0].lower(), p[1] if len(p) > 1 else ""

            if cmd == "q": break
            elif cmd == "t":
                try: m = float(arg)
                except: m = 30
                self.tick(m); self.render()
            elif cmd == "h":
                try: m = float(arg) * 60
                except: m = 60
                self.tick(m); self.render()
            elif cmd == "d":
                try: m = float(arg) * 1440
                except: m = 1440
                self.tick(m); self.render()
            elif cmd == "m":
                self.user_msg(arg or "在吗"); self.tick(2); self.render()
            elif cmd == "s": self.render()
            elif cmd == "r":
                self.state = ChiguoState(self.cfg)
                self.composer = MessageComposer(self.state, self.cfg.get("composer", {}))
                self.sent = []
                self.sim_now = datetime.now(CST)
                print(f"{Color.YEL}🔄 已重置{Color.R}")
                self.render()
            elif cmd == "help":
                print("[回车]推进30min | t/h/d N | m 消息 | s 状态 | r 重置 | q 退出")
            else:
                print(f"未知: {cmd}")

        self.state.save()
        print("💤 已停止。")


if __name__ == "__main__":
    Demo().run()
