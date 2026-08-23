"""cli.handlers.consolidate — --consolidate 分支。"""
import sys


def handle_consolidate(args) -> bool:
    if not args.consolidate:
        return False
    from decision.engine import DecisionEngine
    sys.exit(DecisionEngine().cli_consolidate())
    return True  # noqa: unreachable
