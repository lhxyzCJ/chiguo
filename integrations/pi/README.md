# 迟菓 Pi extension（v2 runtime 客户端骨架）

Phase 7 预备（Issue #477）：Pi 作 Agent Runtime 时，由本扩展在回合开始注入迟菓上下文、
在消息定稿时回写转录事实（对应 `docs/architecture-v2.md` §3.3 / §3.11）。

## 加载

```bash
pi --extension <repo>/integrations/pi/extension/chiguo-context.mjs
```

环境变量：`CHIGUO_RUNTIME_URL`（默认 `http://127.0.0.1:8790`）、
`CHIGUO_RUNTIME_TIMEOUT_MS`（单请求超时，默认 `500`）。

## 端点契约（v2 runtime，仅 127.0.0.1 回环）

- `GET /context?session=<Pi session id>` → JSON，字段为任意子集：
  `personality` / `relationship` / `agenda` / `memories` / `intent` / `world`
  （值为 string 或 string[]；`memories` 也接受 `{text}` 项）。非空字段渲染为
  system prompt 的 `chiguo-context` 段落，经 `before_agent_start` 增量附加
  （修改 prompt sections，绝不替换整个 system prompt）。
- `POST /turn`，body `{session, role, text, at}`：`role` 为 `user|assistant`，
  `text` 为纯文本，`at` 为消息时间戳（epoch ms）。Pi 不校验响应体。

## fail-open

runtime 不可达 / 超时 / 非 JSON / HTTP 非 2xx：跳过本次注入或回写，不阻塞 Pi；
stderr 一行 `[chiguo-context]` 告警，进程内 60s 限频。测试：`node tests/test_pi_extension.mjs`。
