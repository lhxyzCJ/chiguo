"""app.runtime — v2 运行时组件（事件的增量消费者）。

- reducer：events → 物化状态（affect/relationship/承诺/话题/观测）；
- extractor：message.received → 结构化事件（确定性规则提取，无 LLM）。

消费者一律以 runtime_checkpoints 游标增量驱动：单写者、可重放、失败不阻断主链。
"""
