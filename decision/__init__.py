"""decision — 迟菓决策引擎核心包（拆自 chiguo_daemon.py）。

职责分层：
  - decision.base      基础infra（构造/基座锚定/热重载/快照）
  - decision.core      核心决策（evaluate/tick/idle/触发生成）
  - decision.context   上下文构建（_build_context，agent 生成消息用）
  - decision.engine    组合出口 DecisionEngine（所有 mixin 汇合点）
记账/审计 mixin 归属 ops.engine_ops（AccountingMixin）。
"""
