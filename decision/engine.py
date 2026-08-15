"""decision.engine — DecisionEngine 组合出口。

由 base/core/context/ops + runner.loop 五个 mixin 组合成完整引擎。
chiguo_daemon.py 与各测试经 `from chiguo_daemon import DecisionEngine` 访问。
"""


from decision.base import DecisionEngineBase
from decision.core import DecisionCoreMixin
from decision.context import ContextMixin
from ops.engine_ops import AccountingMixin
from runner.loop import LoopSenderMixin


class DecisionEngine(DecisionCoreMixin, ContextMixin, AccountingMixin,
                     LoopSenderMixin):
    """纯决策引擎：评估状态，输出结构化触发决策。

    六个职责 mixin 组合：
      - DecisionEngineBase  基础设施（构造/基座锚定/热重载/快照）
      - DecisionCoreMixin   核心决策（evaluate/tick/idle/触发生成）
      - ContextMixin        上下文构建（agent 生成消息用）
      - AccountingMixin     记账/审计（用户消息/发送/召回/consolidate）
      - LoopSenderMixin     loop 发送内聚（生成→发送→记账 + U2 健康记账）
    """
    pass
