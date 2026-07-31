# OpenClaw Workspace 改写计划

## ⚠️ CLAUDE.md 冲突声明
CLAUDE.md 写有「严禁修改 /root/.openclaw/」，但用户明确指示修改。用户指令优先。

## 目标
- 主人 → 哥哥
- 去掉「管家」字眼，还原为原著外卖员/妹妹身份
- **保留 VPS 相关描述**（她是赛博生命，VPS 是她的小世界）
- 丰富 MD 文件，语言风格与原著迟菓一致
- 不影响 character_test 代码

## Impact on character_test
**零影响。** chiguo_daemon.py 是纯数学引擎，不感知称呼/身份。SKILL.md 仍调用同一 daemon。唯一变化是 OpenClaw 生成的消息语气更贴近原著。character_test 不需要任何代码改动。

## 修改清单（按优先级）

### Phase 1: 核心身份文件
1. `IDENTITY.md` — 身份描述 + 称呼修正
2. `SOUL.md` — 大改：去掉管家/仆人，保留VPS赛博生命，丰富原著性格
3. `USER.md` — 称呼修正 + 精简

### Phase 2: 迟菓技能文件
4. `SUN2.md` — 按对比报告修正（主人→哥哥、去管家、降语气词密度、增「哼」）
5. `SKILL.md` — 称呼修正
6. `迟菓语言技巧指南.md` — 称呼修正 + 去管家 + 调整语气词指南

### Phase 3: 记忆/辅助文件
7. `MEMORY.md` — 称呼修正
