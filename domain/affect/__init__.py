"""domain.affect — 情绪域模型（re-export）。"""
from domain.affect.model import (
    AffectState,
    apply_character_send,
    apply_user_message,
    initial,
    refund_send,
    tick,
)

__all__ = [
    "AffectState",
    "apply_character_send",
    "apply_user_message",
    "initial",
    "refund_send",
    "tick",
]
