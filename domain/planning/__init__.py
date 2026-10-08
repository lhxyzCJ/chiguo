"""domain.planning — 机会发现 / 驱动力评估 / planner（v2 决策层）。

规划层是**纯函数**：输入为观测/状态快照，输出为建议（不谈发送执行）。
Opportunity 描述「什么时机值得行动」，不决定行动与否——那是 planner 的职责。
"""
