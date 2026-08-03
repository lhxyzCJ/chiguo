# Claude Code Rules — Chiguo Proactive Message System

> Auto-generated 2026-07-02 from full codebase audit; refreshed 2026-08-03. 36 py + 10 script runners, zero framework, pure Python stdlib.
> **Iron law**: decision/generation separation. Daemon outputs JSON. pi-agent generates messages (Phase 4).

---

## 1. Build & Test

```bash
# Run ALL tests: 36 py + 10 script runners (every runner exits non-zero on failure)
node tests/test_pi_run.mjs && node tests/test_bridge_askpi.mjs && node tests/test_bridge_cmd.mjs && \
node tests/test_bridge_health.mjs && node tests/test_bridge_schedule.mjs && \
bash tests/test_install_pi.sh && bash tests/test_wechat_bridge.sh && bash tests/test_netease_api.sh && \
bash tests/test_tick_health.sh && \
uv run python tests/test_chiguo_math.py && uv run python tests/test_holiday_parser.py && \
uv run python tests/test_schedule_parser.py && \
uv run python tests/test_integration.py && uv run python tests/test_monitor.py && \
uv run python tests/test_eventbus.py && uv run python tests/test_personality.py && \
uv run python tests/test_bayesian.py && uv run python tests/test_composer.py && \
uv run python tests/test_ebbinghaus.py && uv run python tests/test_longing.py && \
uv run python tests/test_escape_valve.py && uv run python tests/test_feedback.py && \
uv run python tests/test_trigger.py && uv run python tests/test_topics.py && \
uv run python tests/test_circadian.py && uv run python tests/test_followup.py && \
uv run python tests/test_netease_proof.py && uv run python tests/test_netease_service.py && \
uv run python tests/test_envcheck.py && uv run python tests/test_composer_trade.py && \
uv run python tests/test_personality_init.py && uv run python tests/test_toml_binding.py && \
uv run python tests/test_adapt_personality.py && uv run python tests/test_pi_health.py && \
uv run python tests/test_anniversary.py && uv run python tests/test_schedule_override.py && \
uv run python tests/test_day_plan.py && uv run python tests/test_recall.py && \
uv run python tests/test_attention_tiers.py && uv run python tests/test_availability.py && \
uv run python tests/test_trigger_scale.py && uv run python tests/test_isolation.py && \
uv run python tests/test_schedule_plan.py && uv run python tests/test_schedule_cli.py && uv run python tests/test_docs_sync.py

# Single file
python3 tests/test_monitor.py

# Decision engine (single eval → JSON to stdout)
python3 chiguo_daemon.py

# Interactive demo (no LLM, templates only)
python3 chiguo_demo.py

# Monitoring
python3 chiguo_daemon.py --stats 7     # 7-day stats
python3 chiguo_daemon.py --alerts      # anomaly detection
python3 chiguo_daemon.py --monitor     # full report
python3 chiguo_monitor.py --summary    # human-readable summary
python3 chiguo_monitor.py --health     # system health check
python3 chiguo_monitor.py --alerts     # alert list
python3 chiguo_monitor.py --alerts-all # incl. resolved
python3 chiguo_watchdog.py             # standalone health (exit 0/1/2)

# Maintenance
python3 update_holidays.py 2027 --solar --force  # generate holiday data

# Log rotation (auto-called by daemon init)
python3 chiguo_rotation.py --force
```

**No build step. No pip install.** Python 3.14-only (PEP 758 bracketless `except E1, E2:`, deferred annotations — do NOT add `from __future__ import annotations`). Optional: `openpyxl` (schedule), `lancedb` (memory bridge). Note: `memory_bridge.py` lazy-imports `lancedb` inside `_ensure_table()`; when absent it runs with `available=False` and memory queries degrade gracefully — the daemon never crashes on missing lancedb.

---

## 2. Architecture — File Dependency Tree

```
chiguo_daemon.py (DecisionEngine — 1580 lines)
├── chiguo_math.py          Pure functions: sigmoid, decay, recover, dynamic_lambda, Hawkes, longing
├── chiguo_state.py         ChiguoState + ChiguoEmotion + CooldownState (1701 lines)
│   ├── chiguo_personality.py  PersonalityTraits (8 dims) + PersonalityDelta + Deltas (231 lines)
│   ├── chiguo_bayesian.py     UserStateEstimator (6 states) + BayesianLearner
│   ├── schedule/ 包        schedule 数据面 parser.py (xskb.xlsx → schedule_cache.json → query) + parsing.py 纯解析 + query.py 策略 + holiday.py (节假日) + anniversary.py (纪念日) + override_store/plan_store/api (覆盖/计划/澄清存储) + sources/day_plan/resolve_when/attention/recall (检索与安排) + confirm + replan
│   ├── memory_bridge.py       LanceDB read-only (lazy import, available=False degrade) + Ebbinghaus forgetting (506 lines)
│   └── chiguo_circadian.py    dual-bucket sleep-window learning (weekday/weekend + active-time merging)
├── chiguo_trigger.py       evaluate_triggers() → 13 trigger types (incl. v7 follow_up), sigmoid-weighted
├── chiguo_topics.py        TopicPicker — 8 sources (incl. v9 netease), Ebbinghaus-weighted memory
├── chiguo_composer.py      MessageComposer — Intent × Cue × Vibe (389 lines)
├── chiguo_eventbus.py      EventBus — lightweight pub/sub singleton (subscribe/publish only)
├── chiguo_rotation.py      Monthly log rotation → archive/
├── chiguo_version.py       Project version single source (VERSION="1", +0.1 per round)
└── chiguo_monitor.py       ChiguoMonitor + AlertManager + DecisionIndex (1196 lines)

Supporting (not imported by daemon):
├── chiguo_demo.py          Interactive terminal demo (template-only, no LLM) (191 lines)
├── chiguo_watchdog.py      Standalone health checks (cron/systemd timer)
├── chiguo_envcheck.py      Read-only env readiness check (Python/pi/ollama/auth/LanceDB/netease/data, exit 0/1/2)
├── netease/                Netease package: bridge.py (NeteaseBridge 数据面, upstream: api-enhanced v4.39.0, localhost:3000) + service.py (NeteaseService 策略层 DI) + 运行时文件(锚定 <base_dir>/netease/)
├── solar_terms.py          24 solar terms lookup (±1 day window) (85 lines)
└── update_holidays.py      Generate holidays.json + solar_terms.json for any year
```

---

## 3. Core Decision Flow (chiguo_daemon.py evaluate())

```
evaluate(now)
├── _maybe_reload_config()     Check TOML mtime → hot-reload
├── _tick(now)                 Wall-clock → state.tick(hours)
│   ├── Monotonic clock guard  (backward jump returns without tick(); forward >24h → dampen 50%)
│   └── Naive datetime → CST   (_parse_tz helper)
├── Bayesian infer             state.infer_user_state(now) → should_send_bayesian (happens in evaluate(), not can_send())
├── can_send() gate
│   ├── Daily limit            (max 4 active / 2 silent)
│   ├── Min interval           30 min default
│   ├── Energy check           primary gate 12, override min 5 via rate_energy_override
│   ├── Quiet hours            00:00-8:00
│   ├── Busy suppression       user busy → suppress_hours
│   ├── Bayesian block         evaluated in evaluate(), not can_send()
│   └── Longing overflow       held_count>3 + acc_lam>=base*1.5 + anxiety<threshold → allow
├── If CANNOT send → idle
│   ├── longing_accumulate()   Only runs for "no_trigger" and "user_busy" idle reasons (not all idle)
│   ├── save state
│   └── return idle decision + next_evaluation_at
├── If CAN send → evaluate_triggers(state, now)
│   ├── Ritual: special(3.0), morning(2.5), night(2.0), memory(2.0)
│   ├── Emotion: lonely_low/mid/high (softmax normalized), anxiety, playful, reflect, longing
│   ├── Weighted random choice (sigmoid weights)
│   └── Safety valve: lonely_high→lonely_mid (level 1), all→soft (level 2)
├── If trigger fires → build_context() + on_character_message() + save + log
│   ├── Composer: select_combo(trigger_type, now) → Intent + Cue + Vibe
│   ├── Topic injection: 70% chance for lonely triggers, forced at 3 consecutive
│   └── character_rules: ⑦ in code (⑧⑨ only in condensed context dict); kernel layer pauses rule ①
└── _dynamic_sleep_interval()  Compute optimal next check (for --loop mode)
```

---

## 4. 5-Dimension Emotion Engine (chiguo_state.py)

| Dimension | Initial | Equilibrium | Half-life | User reply effect |
|-----------|---------|-------------|-----------|-------------------|
| loneliness | 15 | → 100 | 40h | decay drop (0.35h) |
| affection | 55 | → 0 | 500h | Mild gain |
| anxiety | 40 | → 100 | 30h | decay drop (0.5h) |
| energy | 85 | → 100 | 8h | Mild gain |
| tsundere_index | 70 | → personality baseline | — | Drops on reply |

**Personality layers** (dominant_layer property):
- **Shell** (loneliness≤50, anxiety≤70): Genki, bright, active. Rule ① active.
- **Middle** (loneliness>50, anxiety≤70): Stubborn, mouth-hard. Rule ① active.
- **Kernel** (anxiety>70 or loneliness>80): Fragile, vulnerable. Rule ① PAUSED.

**Key formulas**:
- Decay: `value * 2^(-t/half_life)`
- Recovery: `current + gap * (1 - 2^(-t/half_life))`
- Event rate: `current_lambda()` — base × sigmoid(loneliness) × sigmoid(anxiety) × availability × Hawkes × no-reply backoff
- Dynamic lambda: `base * sigmoid(loneliness) * sigmoid(anxiety) * availability * modifiers`
- Hawkes: `base_mu + Σ(alpha * exp(-beta * (t - t_i)))`
- Longing accumulate: `new_lambda += growth_factor * max(held_count, 1)`, capped at `base_lambda * max_lambda_multiplier`; blocked if `anxiety >= anxiety_block_threshold`

---

## 5. Trigger System (chiguo_trigger.py)

13 trigger types (v7: + follow_up 接话茬), all sigmoid-weighted random selection (no hard thresholds):

| Trigger | Weight | Condition |
|---------|--------|-----------|
| special | 3.0 × ritual_scale | Today is special date (birthdays) |
| morning | 2.5 × ritual_scale | 8-10am, 10% random gate, not sent today |
| night | 2.0 × ritual_scale | 8-9pm, 12% probability, not sent today |
| json_memory | 2.0 × ritual_scale | JSON memory with trigger_at in ±10min window |
| lancedb_memory | 1.5 × ritual_scale | 8% random gate when silent>6h |
| meal | 0.8 × ritual_scale | 11-12,17-19h, 5% probability |
| lonely_low | softmax(loneliness) | sigmoid gate at midpoint 38 |
| lonely_mid | softmax(loneliness) | sigmoid gate at midpoint 55 |
| lonely_high | softmax(loneliness) | sigmoid gate at midpoint 78 |
| anxiety | softmax(anxiety) | sigmoid gate at midpoint 58 |
| playful | extraversion-modulated | energy>70, 2h<silent<48h, free time |
| reflect | neuroticism-gated | affection>70, silent<2h, energy>60, neuroticism<70 gate |
| longing | overflow signal | is_longing_overflow() condition |

**Personality modulation**: tsundere INCREASES lonely_low/mid weights, DECREASES lonely_high (no direct effect on anxiety); extraversion boosts playful; neuroticism (<70) gates reflect.

---

## 6. Topic Injection (chiguo_topics.py)

8 sources, weighted random, triggered on lonely triggers (70% chance, forced at 3 consecutive):

| Source | Weight | Description |
|--------|--------|-------------|
| schedule | 0.30 | Class schedule, holiday, weekend status |
| memory | 0.25 | LanceDB with Ebbinghaus forgetting re-ranking |
| general | 0.25 | Time-of-day generic topics |
| weather_season | 0.20 | Month-based season hints |
| anniversary | 0.15 | Today's anniversaries + upcoming within 7 days |
| solar_terms | 0.10 | ±1 day window around 24 solar terms |
| preference_followup | 0.10 | LanceDB user preference category memories |
| netease | 0.12 | NeteaseService strategy layer (v9): music topic, peek/consume quota |

**Ebbinghaus**: `R = e^(-t / (strength * importance))`, clamped to `[min_weight, 1.0]`. Default strength=168h (7-day half-life), min_weight=0.1.

---

## 7. Message Composer (chiguo_composer.py)

Intent × Cue × Vibe three-layer system:

- **Layer 1 (Intent)** — 20%: What to express (~36 intents across 13 trigger types)
- **Layer 2 (Intent + Cue)** — 50%: How to express (8 personality styles: tsundere_classic/soft/cool, dere_dere, playful_bubbly, anxious_clingy, caring_gentle, cool_mysterious)
- **Layer 3 (Intent + Cue + Vibe)** — 30%: Atmosphere (13 vibes: early_morning, morning, noon, afternoon, evening, night, late_night, weekend variants, holiday, rainy, sunny, exam_season)

**Cue selection modulated by**: tsundere_intensity, extraversion, neuroticism, agreeableness, trigger_type.

---

## 8. Key Patterns & Conventions

### State Persistence
- **Atomic write**: `.tmp` file → validate JSON → `os.replace()` to target
- **Backup**: `.bak` copy before overwrite
- **Checksum**: SHA256 in `_checksum` field (validates but doesn't reject on mismatch)
- **Audit log**: `chiguo_state_audit.jsonl` records corruption events
- **tick_seq**: Monotonic counter, watchdog compares for forward progress
- **PID lock**: `chiguo_loop.pid` prevents double-start in `--loop` mode

### Streaming JSONL Parser
- `_iter_decisions()` reads line-by-line — O(n) time, O(1) memory
- Handles missing files, empty files, corrupted lines without crashing
- `DecisionIndex` builds byte-offset index for O(1) query by trigger/date/action

### Config Hot-Reload
- `_maybe_reload_config()` checks TOML mtime before each `evaluate()`
- Recreates state objects, resets `_bayesian_estimator` to None (lazy re-init)
- Only matters for `--loop` mode; cron spawns fresh processes

### Test Isolation
- `tests/test_monitor.py` uses `tempfile.TemporaryDirectory`; other test files are pure-function tests with no shared state
- No test framework: plain `assert` statements, `if __name__ == "__main__"` runner
- `sys.path.insert(0, ...)` to find sibling modules
- `random.seed(42)` for determinism in integration tests
- Fixed times via explicit `datetime(..., tzinfo=CST)`

### Naming Conventions
- Test functions: `test_` prefix, print `"  OK test_name"` on success
- Dataclass fields have defaults; mutable defaults use `field(default_factory=...)`
- Private methods: `_` prefix
- CLI: argparse patterns, JSON to stdout, diagnostics to stderr

---

## 9. Ship of Theseus — Config Parameters (chiguo_proactive.toml)

277 lines, 20 sections: 遗留 section（已废弃，Task 14；仅 wechat_recipient 仍读取）`[memory]` `[character]` `[emotion]` `[sigmoid]` `[trigger]` `[poisson]` `[topic_picker]` `[schedule]` `[circadian]` `[netease]` `[hawkes]` `[cooldown]` `[personality]` `[bayesian]` `[composer]` `[safety]` `[monitor]` `[logging]` `[host]`（pi 调用配置，见 §11）。Key tunables:

| Section | Key params | Effect |
|---------|-----------|--------|
| `[emotion]` | half_life values, event deltas | Emotion dynamics speed |
| `[sigmoid]` | k, midpoint per dimension | Trigger probability curves |
| `[poisson]` | base_lambda (0.25/h) | Message frequency baseline |
| `[hawkes]` | alpha=0.3, beta=0.5 | Self-excitation strength/decay |
| `[cooldown]` | max sends, min interval, longing params | Send gating |
| `[topic_picker]` | 8 source weights | Topic distribution |
| `[circadian]` | confidence threshold, window params | Sleep-window learning (v7/v8) |
| `[netease]` | retry, quotas, source weights | Netease strategy layer (v9) |
| `[composer]` | combo size probs, 8 cue weights | Message composition |
| `[personality]` | 8-dim initial values | Character baseline |
| `[bayesian]` | learning_rate, thresholds | User state inference |
| `[safety]` | crash cooldown 24h, max 2/48h | Crash protection |
| `[schedule]` | quiet hours, semester dates, special dates | Time gating |

---

## 10. Decision Output Schema (chiguo_decisions.jsonl)

**Send decision**:
```json
{
  "action": "send", "version": "1", "msg_id": "12-char hex",
  "trigger": "trigger_type", "intensity": "soft|medium|strong",
  "context": {
    "layer_guidance": "...", "character_rules": "...", "instruction": "...",
    "emotion": {...}, "personality_profile": "...", "personality_source": "...",
    "character": "迟菓", "schedule_hint": "...", "situation": "...", "layer": "...",
    "combo": {"combo_string": "..."},
    "silent_hours": 0.0, "poisson_lambda": 0.0, "accumulated_lambda": 0.0,
    "trigger_type": "...", "intensity": "..."
  },
  "emotion": {"loneliness": 0.0, "affection": 0.0, "anxiety": 0.0, "energy": 0.0, "tsundere_index": 0.0},
  "cooldown": {"messages_without_reply": 0, "messages_today": 0},
  "personality": {"tsundere_intensity": 70, ...}
}
```

**Idle decision**: `action: "idle"` (also carries top-level `version`), adds `reason` (idle reason: user_sleeping/user_busy/daily_limit/low_energy/min_interval/quiet_hours/no_trigger/busy_suppressed/sleeping_guard), `next_evaluation_at`, and `state`/`bayesian`.

---

## 11. LLM Host Integration (Phase 4 — pi-agent)

> Current architecture: system crontab `chiguo-tick.sh` (send side, session `chiguo-send`) + wechat-bridge
> `askPi` (reply side, session `chiguo-main`) + `scripts/pi-run.mjs` + `scripts/install_pi.sh`.
> See `doc/PI_INTEGRATION.md`.

### Send side — trigger-script gate (zero model calls on idle)
1. **Cron**: system crontab `*/15 * * * *` runs `scripts/chiguo-tick.sh` (send side, session `chiguo-send`; registered by `scripts/install_pi.sh`)
2. Trigger script runs `<repo>/.venv/bin/python chiguo_daemon.py --compact` with no model execution
3. `action: "idle"` → `{fire: false}` (~90% of evaluations never wake the agent)
4. `action: "send"` → `{fire: true, message: <decision JSON>}` → agent generates 1-3 sentence WeChat message using **SUN2.md** personality + daemon context → sends via `curl --noproxy '*' -X POST http://127.0.0.1:18790/send` (wechat-bridge) → writes back `--record-send <msg_id> --text <text> [--trigger <trigger>] [--intensity <intensity>]` (or `--send-result` on failure)

### Reply side — bridge askPi
1. WeChat message arrives → `bridge.mjs` runs deterministic `--user-msg` on arrival; special-command detection (`command-detect.mjs`: anniversary/break rules, no pi) → otherwise `askPi` (`pi-run.mjs --prompt <原文> --analysis-mode`, session `chiguo-main`)
2. Agent analyzes emotion (warmth/effort/attention/suppress_hours) → updates daemon via `--user-msg --analysis`
3. Agent replies naturally using SUN2.md personality. Recording: bridge deterministically runs `--user-msg` (no analysis) on arrival; the askPi `--user-msg --analysis` call is deduped by daemon `recv_dedup` (same text within 600s → analysis-only upgrade, no double counting)

### Schedule-center CLI (daemon subcommands, schedule/ 包)
- `chiguo_daemon.py --attention` — T1/T2/T3 注意力快照（回复侧注入，零写；失败降级继续 askPi）
- `chiguo_daemon.py --schedule-recall <query>` — 安排回忆检索（日期或关键词，A4 形状）
- `chiguo_daemon.py --schedule-change <json>` — 写安排（reminder/add/cancel/move/exam_week/remove；畸形 JSON → bad_json 不写入；ApiRejection → H5 文案）
- `python -m schedule.replan --check` — 复盘只读检查明日计划；`python -m schedule.holiday [YYYY-MM-DD]` — 节假日查询

### SUN2.md Personality Constitution (283 lines)
- **3-layer structure**: 喧闹外壳 → 倔强中层 → 脆弱内核
- **3-stage tsundere protocol**: Push away (MANDATORY) → Accept + belittle → Quiet truth leak
- **Signature**: 哼 is core particle. ~ on 30-40% dialogue. 喵 only for cats. 嘻嘻 extremely rare (10/17000 lines).
- **Anti-patterns table**: 15 forbidden behaviors with correct alternatives
- **Self-check**: 15-item role consistency checklist

### Skill files (allowed security boundary)
- **Repo**（Phase 4 起唯一权威）：`personality/SUN2.md`、`personality/迟菓语言技巧指南.md`（随仓库部署，pi-run 注入）

Note: v4 residue `scripts/on-user-msg.sh` and `.claude/settings.json` UserPromptSubmit hooks have been removed (backed up to `.bak`).

---

## 12. Runtime Files (privacy data — login state/conversation logs/personal data — is never committed; kept locally only)

Note: privacy data (WeChat login state, conversation logs `chiguo_messages.jsonl`/`chiguo_decisions.jsonl`, `data/` personal files) is **never committed** — kept locally only, history rewritten (2026-08-02 security pass).

| File | Writer | Purpose |
|------|--------|---------|
| `chiguo_state.json` | chiguo_state.py | Persistent emotion state + last_tick + tick_seq + checksum |
| `chiguo_state.json.bak` | chiguo_state.py | Pre-write backup |
| `chiguo_state.json.tmp` | chiguo_state.py | Atomic write staging |
| `chiguo_decisions.jsonl` | chiguo_daemon.py | Append-only decision log |
| `chiguo_messages.jsonl` | chiguo_daemon.py | Human-readable conversation log (v5) |
| `chiguo_state_audit.jsonl` | chiguo_state.py | Corruption/recovery audit trail (v5) |
| `chiguo_alerts.json` | chiguo_monitor.py | AlertManager persisted state |
| `chiguo_watchdog_state.json` | chiguo_watchdog.py | Last seen tick_seq for stall detection |
| `chiguo_loop.pid` | chiguo_daemon.py | PID lock file |
| `schedule_cache.json` | schedule/parser.py | Parsed xskb.xlsx cache |
| `schedule_overrides.json` | schedule/override_store.py | Manual schedule overrides (atomic 0600) |
| `schedule_plan.json` | schedule/plan_store.py | Daily plan, replan-generated (atomic 0600) |
| `schedule_clarify.json` | wechat-bridge/bridge.mjs | Clarify record (writeClarify atomic 0600, 6h expiry) |
| `anniversaries.json` | schedule/anniversary.py | Anniversary records (countdown deprecated, migrated to reminder) |
| `break_state.json` | chiguo_daemon.py | Vacation override (written by --break CLI) |
| `holidays.json` | update_holidays.py | Override holiday data for future years |
| `solar_terms.json` | update_holidays.py | Override solar terms for future years |
| `netease/netease_cache.json` | netease/bridge.py | Daily song recommendations cache |
| `netease/netease_cookie.txt` | netease/bridge.py | Netease API auth cookie (chmod 600) |
| `netease/recent_play_cache.json` | netease/bridge.py | Recent-play cache (v8, atomic write, 15-min TTL) |
| `netease/netease_health.json` | netease/service.py | Netease health/quota state (v9, atomic write) |

---

## 13. Known Design Decisions (not bugs)

- **M3**: Config hot-reload resets Bayesian estimator → conservative, prevents stale config
- **M5**: Ritual trigger weights (2.5-3.0) dominate emotion weights (0.01-0.9) → by design; tune `ritual_weight_scale` to adjust
- **Hawkes dt==0 exclusion**: Events at `now` are the current event itself, not historical excitation — intentional
- **Lunar holiday estimation in update_holidays.py**: ~11 day/year drift for non-2027 years. Requires manual update of `KNOWN_HOLIDAYS` when State Council notice published
- **Solar terms estimation**: ~6h/year drift. Requires annual update of `SOLAR_TERMS_2027`

---

## 14. Pitfalls & Gotchas

1. **File already read**: Edit tool requires Read first. When Read hook blocks (claude-mem down), use Bash/python for edits.
2. **Timezone**: Always use `CST = timezone(timedelta(hours=8))`. Naive datetimes get auto-completed via `_parse_tz()`.
3. **TOML types**: `rate_energy_min = 5.0` must be float. Comments must match values.
4. **LanceDB optional**: All memory queries gracefully degrade to `[]`. Tests mock or skip.
5. **xskb.xlsx missing**: Schedule fallback → availability=0.85 (treated as free time).
6. **Test order**: Integration tests need `chiguo_proactive.toml` in CWD. Run from the repo root.
7. **Ritual weight scale**: `evaluate_triggers()` multiplies all ritual weights by `ritual_weight_scale`. Set to 0.3 to balance with emotion weights.
8. **character_rules ⑦ vs condensed**: Full rules (guidance variable) missing 喵/嘻嘻. Condensed version (context dict) has them. Both now fixed (2026-07-02).
9. **EventBus return type**: `publish()` returns `list[Any]` — backward compatible. Failed handlers get `None` appended (not silently dropped).
10. **Bayesian normalization**: `update_from_label()` normalizes cached P(obs|state) to sum=1.0. Uncached values default to 0.05 but are not in the normalization group.

---

## 15. File Quick Reference

| File | Lines | Purpose | Key exports |
|------|-------|---------|-------------|
| chiguo_math.py | 167 | Pure math | sigmoid, decay, recover, dynamic_lambda, hawkes_intensity, longing_*, weighted_trigger_choice |
| chiguo_state.py | 1701 | State engine | ChiguoState, ChiguoEmotion, CooldownState |
| chiguo_daemon.py | 1580 | Orchestrator | DecisionEngine, main() |
| chiguo_trigger.py | 370 | Trigger selection | evaluate_triggers(), Trigger |
| chiguo_topics.py | 372 | Topic injection | TopicPicker |
| chiguo_composer.py | 389 | Message composition | MessageComposer |
| chiguo_personality.py | 231 | Personality system | PersonalityTraits, PersonalityDelta, PersonalityDeltas |
| chiguo_bayesian.py | 474 | User inference | UserStateEstimator, BayesianLearner |
| chiguo_eventbus.py | 56 | Pub/sub | EventBus, get_eventbus(), reset_eventbus() |
| chiguo_monitor.py | 1196 | Analytics | ChiguoMonitor, AlertManager, DecisionIndex |
| chiguo_watchdog.py | 294 | Health checks | run_all_checks(), cli() |
| chiguo_rotation.py | 165 | Log rotation | rotate_if_needed(), force_rotate() |
| chiguo_demo.py | 191 | Interactive demo | Demo class |
| chiguo_version.py | 5 | Project version | VERSION |
| memory_bridge.py | 506 | Memory access | MemoryBridge |
| schedule/ 包 | — | Schedule/holiday/anniversary/arrangements | parser.py: ScheduleParser; parsing.py; query.py; holiday.py: HolidayParser; anniversary.py: AnniversaryManager; override_store.py; plan_store.py; api.py; sources.py; day_plan.py; resolve_when.py; attention.py; recall.py; confirm.py; replan.py |
| solar_terms.py | 85 | Solar terms | SolarTerms |
| netease/ | 939 | Netease package | bridge.py: NeteaseBridge (login/fetch_recent_play/fetch_daily_songs); service.py: NeteaseService (DI) |
| chiguo_envcheck.py | 178 | Env readiness | run_checks() |
| update_holidays.py | 234 | Holiday gen | generate() |
| chiguo_proactive.toml | 277 | Config | All parameters |

---

## 16. Security Boundary

**READ-ONLY**: `~/.pi-agent/memory/lancedb-pro` LanceDB — accessed only via `memory_bridge.py`.

---

## 17. Post-Change Checklist

After ANY code change:
1. Run affected test files
2. Run full suite if touching math/state/daemon
3. Update `doc/SYSTEM.md` if architecture/CLI/config changed
4. Update relevant memory files in `/root/.claude/projects/-root-character-test/memory/`
