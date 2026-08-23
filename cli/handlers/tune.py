"""cli.handlers.tune — --tune 分支。"""
import json


def handle_tune(args) -> bool:
    if not args.tune:
        return False
    from decision.engine import DecisionEngine
    engine = DecisionEngine()
    latencies = engine.state.cooldown.reply_latencies
    if len(latencies) < 5:
        print(json.dumps({
            "action": "tune",
            "error": f"需要至少 5 次交互数据，当前 {len(latencies)} 次",
            "hint": "发送几条消息并等待哥哥回复，积累数据后再试",
        }, ensure_ascii=False))
    else:
        import statistics
        median_h = statistics.median(latencies)
        avg_h = sum(latencies) / len(latencies)
        current_base = engine.config.get("poisson", {}).get("base_lambda", 0.25)
        if median_h < 0.3:
            suggestion = "increase"
            new_base = min(0.5, current_base * 1.3)
            reason = f"哥哥回复很快（中位数 {median_h:.1f}h），可以更频繁"
        elif median_h > 3.0:
            suggestion = "decrease"
            new_base = max(0.05, current_base * 0.7)
            reason = f"哥哥回复较慢（中位数 {median_h:.1f}h），减少频率"
        else:
            suggestion = "keep"
            new_base = current_base
            reason = f"回复节奏适中（中位数 {median_h:.1f}h），保持当前参数"
        print(json.dumps({
            "action": "tune",
            "latency_count": len(latencies),
            "median_hours": round(median_h, 2),
            "avg_hours": round(avg_h, 2),
            "current_base_lambda": current_base,
            "suggestion": suggestion,
            "suggested_base_lambda": round(new_base, 3),
            "reason": reason,
            "hint": f"手动修改 chiguo_proactive.toml [poisson] base_lambda = {new_base:.3f}",
        }, ensure_ascii=False, indent=2))
    return True
