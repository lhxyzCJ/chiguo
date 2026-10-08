"""domain.relationship — 关系域模型（re-export）。"""
from domain.relationship.model import (
    RelationshipState,
    apply_event,
    decay,
    initial,
)

__all__ = [
    "RelationshipState",
    "apply_event",
    "decay",
    "initial",
]
