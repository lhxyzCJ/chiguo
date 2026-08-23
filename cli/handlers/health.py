"""cli.handlers.health — --health 分支。"""
import json
from datetime import datetime
from chiguo_time import CST



def handle_health(args) -> bool:
    if not args.health:
        return False
    from decision.engine import DecisionEngine
    health_engine = DecisionEngine()
    sp = health_engine.state.state_path
    healthy = False
    last_tick = None
    hours_ago = None
    error = None
    if sp.exists():
        try:
            data = json.loads(sp.read_text())
            last_tick = data.get("last_tick")
            if last_tick:
                lt = datetime.fromisoformat(last_tick)
                if lt.tzinfo is None:
                    lt = lt.replace(tzinfo=CST)
                hours_ago = (datetime.now(CST) - lt).total_seconds() / 3600
                healthy = hours_ago < 6
                if not healthy:
                    error = f"last_tick {hours_ago:.1f}h ago (threshold: 6h)"
            else:
                error = "no last_tick in state file"
        except Exception as e:
            error = f"state file read error: {e}"
    else:
        error = "state file not found (daemon never run?)"
    print(json.dumps({
        "healthy": healthy,
        "last_tick": last_tick,
        "hours_ago": round(hours_ago, 1) if hours_ago else None,
        "error": error,
    }, ensure_ascii=False))
    return True
