# Claude Code Rules — Chiguo Proactive Message System

> Auto-generated 2026-07-02 from full codebase audit. 159 tests, zero framework, pure Python stdlib.
> **Iron law**: decision/generation separation. Daemon outputs JSON. OpenClaw generates messages.

---

## 1. Build & Test

```bash
# Run ALL 159 tests (10 files)
python3 test_chiguo_math.py && python3 test_holiday_parser.py && \
python3 test_integration.py && python3 test_monitor.py && \
python3 test_eventbus.py && python3 test_personality.py && \
python3 test_bayesian.py && python3 test_composer.py && \
python3 test_ebbinghaus.py && python3 test_longing.py

# Single file
python3 test_monitor.py

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

**No build step. No pip install.** Dependencies: Python 3.11+ stdlib (`tomllib`). Optional: `openpyxl` (schedule), `lancedb` (memory bridge). Note: `memory_bridge.py` hard-imports `lancedb` at module level; will crash on import if absent. Install lancedb or accept import failure.

---

## 2. Architecture — File Dependency Tree

```
chiguo_daemon.py (DecisionEngine — 1267 lines)
├── chiguo_math.py          Pure functions: sigmoid, decay, recover, Poisson, Hawkes, longing
├── chiguo_state.py         ChiguoState + ChiguoEmotion + CooldownState (1310 lines)
│   ├── chiguo_personality.py  PersonalityTraits (8 dims) + PersonalityDelta + Deltas (242 lines)
│   ├── chiguo_bayesian.py     UserStateEstimator (6 states) + BayesianLearner
│   ├── schedule_parser.py     xskb.xlsx → schedule_cache.json → query() (336 lines)
│   ├── holiday_parser.py      7 holidays + 6 makeup days for 2026 (185 lines)
│   └── memory_bridge.py       LanceDB read-only + Ebbinghaus forgetting (477 lines)
├── chiguo_trigger.py       evaluate_triggers() → 13 trigger types (incl. v7 follow_up), sigmoid-weighted
├── chiguo_topics.py        TopicPicker — 7 sources, Ebbinghaus-weighted memory
├── chiguo_composer.py      MessageComposer — Intent × Cue × Vibe (387 lines)
├── chiguo_eventbus.py      EventBus — lightweight pub/sub singleton
├── chiguo_rotation.py      Monthly log rotation → archive/
└── chiguo_monitor.py       ChiguoMonitor + AlertManager + DecisionIndex (1167+ lines)

Supporting (not imported by daemon):
├── chiguo_demo.py          Interactive terminal demo (template-only, no LLM) (188 lines)
├── chiguo_generator.py     MessageGenerator (LLM via Ollama + template fallback) (269 lines)
├── chiguo_sender.py        MessageSender (outbox file → OpenClaw WeChat bridge)
├── chiguo_watchdog.py      Standalone health checks (cron/systemd timer)
├── anniversary_manager.py  CRUD for anniversaries/countdowns
├── netease_bridge.py       Netease Cloud Music API (QR login, daily recs) (465 lines)
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
│   ├── Quiet hours            22:00-8:00
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
| loneliness | 15 | → 100 | 40h | Poisson drop |
| affection | 55 | → 0 | 500h | Mild gain |
| anxiety | 40 | → 100 | 30h | Poisson drop |
| energy | 85 | → 100 | 8h | Mild gain |
| tsundere_index | 70 | → personality baseline | — | Drops on reply |

**Personality layers** (dominant_layer property):
- **Shell** (loneliness≤50, anxiety≤70): Genki, bright, active. Rule ① active.
- **Middle** (loneliness>50, anxiety≤70): Stubborn, mouth-hard. Rule ① active.
- **Kernel** (anxiety>70 or loneliness>80): Fragile, vulnerable. Rule ① PAUSED.

**Key formulas**:
- Decay: `value * 2^(-t/half_life)`
- Recovery: `current + gap * (1 - 2^(-t/half_life))`
- Poisson: `P(event) = 1 - exp(-lambda * interval)`
- Dynamic lambda: `base * sigmoid(loneliness) * sigmoid(anxiety) * availability * modifiers`
- Hawkes: `base_mu + Σ(alpha * exp(-beta * (t - t_i)))`
- Longing accumulate: `new_lambda += growth_factor * max(held_count, 1)`, capped at `base_lambda * max_lambda_multiplier`; blocked if `anxiety >= anxiety_block_threshold`

---

## 5. Trigger System (chiguo_trigger.py)

13 trigger types (v7: + follow_up 接话茬), all sigmoid-weighted random selection (no hard thresholds):

| Trigger | Weight | Condition |
|---------|--------|-----------|
| special | 3.0 × ritual_scale | Today is special date (birthdays) |
| morning | 2.5 × ritual_scale | 8-10am, 10% Poisson, not sent today |
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

7 sources, weighted random, triggered on lonely triggers (70% chance, forced at 3 consecutive):

| Source | Weight | Description |
|--------|--------|-------------|
| schedule | 0.30 | Class schedule, holiday, weekend status |
| memory | 0.25 | LanceDB with Ebbinghaus forgetting re-ranking |
| general | 0.25 | Time-of-day generic topics |
| weather_season | 0.20 | Month-based season hints |
| anniversary | 0.15 | Today's anniversaries + upcoming within 7 days |
| solar_terms | 0.10 | ±1 day window around 24 solar terms |
| preference_followup | 0.10 | LanceDB user preference category memories |

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
- `test_monitor.py` uses `tempfile.TemporaryDirectory`; other test files are pure-function tests with no shared state
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

229 lines, 16 sections: `[openclaw]` `[memory]` `[character]` `[emotion]` `[sigmoid]` `[poisson]` `[topic_picker]` `[schedule]` `[hawkes]` `[cooldown]` `[personality]` `[bayesian]` `[composer]` `[safety]` `[monitor]` `[logging]`. Key tunables:

| Section | Key params | Effect |
|---------|-----------|--------|
| `[emotion]` | half_life values, event deltas | Emotion dynamics speed |
| `[sigmoid]` | k, midpoint per dimension | Trigger probability curves |
| `[poisson]` | base_lambda (0.25/h) | Message frequency baseline |
| `[hawkes]` | alpha=0.3, beta=0.5 | Self-excitation strength/decay |
| `[cooldown]` | max sends, min interval, longing params | Send gating |
| `[topic_picker]` | 7 source weights | Topic distribution |
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
  "action": "send", "msg_id": "12-char hex",
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

**Idle decision**: `action: "idle"`, adds `reason` (idle-only description), `idle_reason` (user_sleeping/user_busy/daily_limit/low_energy/min_interval/quiet_hours/no_trigger), and `next_evaluation_at`.

---

## 11. OpenClaw Integration

### chiguo skill workflow
1. **Cron** triggers agent (interval configured in SKILL.md / OpenClaw cron settings)
2. Agent runs `python3 chiguo_daemon.py`
3. If `action: "send"`: generates 1-3 sentence WeChat message using **SUN2.md** personality + daemon context → sends via `openclaw-weixin` channel
4. If `action: "idle"`: stops

### UserPromptSubmit hook
1. WeChat message arrives → hook runs `on-user-msg.sh`
2. Agent analyzes emotion (warmth/effort/attention/suppress_hours) → updates daemon via `--user-msg --analysis`
3. Agent replies naturally using SUN2.md personality

### SUN2.md Personality Constitution (283 lines)
- **3-layer structure**: 喧闹外壳 → 倔强中层 → 脆弱内核
- **3-stage tsundere protocol**: Push away (MANDATORY) → Accept + belittle → Quiet truth leak
- **Signature**: 哼 is core particle. ~ on 30-40% dialogue. 喵 only for cats. 嘻嘻 extremely rare (10/17000 lines).
- **Anti-patterns table**: 15 forbidden behaviors with correct alternatives
- **Self-check**: 15-item role consistency checklist

### Skill files (allowed security boundary)
- `/root/.openclaw/workspace/skills/chiguo/SKILL.md` (133 lines)
- `/root/.openclaw/workspace/skills/chiguo/SUN2.md` (283 lines)
- `/root/.openclaw/workspace/skills/chiguo/references/迟菓语言技巧指南.md` (153 lines)
- `/root/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh` (8 lines)
- `/root/.openclaw/workspace/agents/main/` — 12 files (IDENTITY, SOUL, MEMORY, etc.)

---

## 12. Runtime Files (all .gitignore)

Note: `.gitignore` currently only covers 7 of 17 runtime files. The 10 missing entries should be added.

| File | Writer | Purpose |
|------|--------|---------|
| `chiguo_state.json` | chiguo_state.py | Persistent emotion state + last_tick + tick_seq + checksum |
| `chiguo_state.json.bak` | chiguo_state.py | Pre-write backup |
| `chiguo_state.json.tmp` | chiguo_state.py | Atomic write staging |
| `chiguo_decisions.jsonl` | chiguo_daemon.py | Append-only decision log |
| `chiguo_messages.jsonl` | chiguo_daemon.py | Human-readable conversation log (v5) |
| `chiguo_state_audit.jsonl` | chiguo_state.py | Corruption/recovery audit trail (v5) |
| `chiguo_message_log.json` | chiguo_sender.py | Sent message history (last 200) |
| `chiguo_alerts.json` | chiguo_monitor.py | AlertManager persisted state |
| `chiguo_watchdog_state.json` | chiguo_watchdog.py | Last seen tick_seq for stall detection |
| `chiguo_loop.pid` | chiguo_daemon.py | PID lock file |
| `schedule_cache.json` | schedule_parser.py | Parsed xskb.xlsx cache |
| `anniversaries.json` | anniversary_manager.py | Anniversary/countdown records |
| `break_state.json` | chiguo_daemon.py | Vacation override (written by --break CLI) |
| `holidays.json` | update_holidays.py | Override holiday data for future years |
| `solar_terms.json` | update_holidays.py | Override solar terms for future years |
| `netease_cache.json` | netease_bridge.py | Daily song recommendations cache |
| `netease_cookie.txt` | netease_bridge.py | Netease API auth cookie (chmod 600) |

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
6. **Test order**: Integration tests need `chiguo_proactive.toml` in CWD. Run from `/root/character_test/`.
7. **Ritual weight scale**: `evaluate_triggers()` multiplies all ritual weights by `ritual_weight_scale`. Set to 0.3 to balance with emotion weights.
8. **character_rules ⑦ vs condensed**: Full rules (guidance variable) missing 喵/嘻嘻. Condensed version (context dict) has them. Both now fixed (2026-07-02).
9. **EventBus return type**: `publish()` returns `list[Any]` — backward compatible. Failed handlers get `None` appended (not silently dropped).
10. **Bayesian normalization**: `update_from_label()` normalizes cached P(obs|state) to sum=1.0. Uncached values default to 0.05 but are not in the normalization group.

---

## 15. File Quick Reference

| File | Lines | Purpose | Key exports |
|------|-------|---------|-------------|
| chiguo_math.py | 189 | Pure math | sigmoid, decay, recover, poisson_*, hawkes_intensity, longing_*, weighted_trigger_choice |
| chiguo_state.py | 1310 | State engine | ChiguoState, ChiguoEmotion, CooldownState |
| chiguo_daemon.py | 1267 | Orchestrator | DecisionEngine, main() |
| chiguo_trigger.py | 279 | Trigger selection | evaluate_triggers(), Trigger |
| chiguo_topics.py | 316 | Topic injection | TopicPicker |
| chiguo_composer.py | 387 | Message composition | MessageComposer |
| chiguo_personality.py | 242 | Personality system | PersonalityTraits, PersonalityDelta, PersonalityDeltas |
| chiguo_bayesian.py | 460 | User inference | UserStateEstimator, BayesianLearner |
| chiguo_eventbus.py | 74 | Pub/sub | EventBus, get_eventbus(), reset_eventbus() |
| chiguo_monitor.py | 1167+ | Analytics | ChiguoMonitor, AlertManager, DecisionIndex |
| chiguo_watchdog.py | 200 | Health checks | run_all_checks(), cli() |
| chiguo_rotation.py | 130 | Log rotation | rotate_if_needed(), force_rotate() |
| chiguo_demo.py | 188 | Interactive demo | Demo class |
| chiguo_generator.py | 269 | Message generation | MessageGenerator |
| chiguo_sender.py | 100 | Message delivery | MessageSender |
| memory_bridge.py | 477 | Memory access | MemoryBridge |
| schedule_parser.py | 336 | Schedule | ScheduleParser |
| holiday_parser.py | 185 | Holidays | HolidayParser |
| solar_terms.py | 85 | Solar terms | SolarTerms |
| anniversary_manager.py | 200 | Anniversaries | AnniversaryManager |
| netease_bridge.py | 465 | Music API | login_qr_flow(), fetch_daily_songs() |
| update_holidays.py | 230 | Holiday gen | generate() |
| chiguo_proactive.toml | 229 | Config | All parameters |

---

## 16. Security Boundary

**STRICTLY FORBIDDEN**: Modify any file under `/root/.openclaw/` EXCEPT:
- `/root/.openclaw/workspace/skills/chiguo/` (SKILL.md, SUN2.md, references/, scripts/)
- `/root/.openclaw/workspace/agents/main/` (IDENTITY.md, SOUL.md, MEMORY.md, etc.)

**READ-ONLY**: `/root/.openclaw/memory/` LanceDB — accessed only via `memory_bridge.py`.

---

## 17. Post-Change Checklist

After ANY code change:
1. Run affected test files
2. Run full suite if touching math/state/daemon
3. Update `doc/SYSTEM.md` if architecture/CLI/config changed
4. Update `doc/IMPROVE.md` if fixing a documented issue
5. Update `MEMORY.md` with date, files modified, description
6. Update relevant memory files in `/root/.claude/projects/-root-character-test/memory/`
