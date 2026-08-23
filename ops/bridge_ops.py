"""ops.bridge_ops — wechat-bridge 发送链路（#376/#275 抽离 dispatch 重复）。

统一 bridge_post + _push_alerts_via_wechat：主动发送 runner/loop 与告警推送
共用 token 注入 + 回环代理绕过（B5），零行为变更。
"""
import json
import os
import sys
import urllib.request

from chiguo_net import build_no_proxy_opener, is_local_host


def bridge_post(bridge_url: str, token: str, path: str, body: dict,
                timeout: float = 10.0) -> dict:
    """POST JSON 至 wechat-bridge，返回解析后的响应 dict。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{bridge_url}{path}", data=data,
        headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-Bridge-Token", token)
    host = urllib.request.urlsplit(req.full_url).hostname or ""
    if is_local_host(host):
        resp = build_no_proxy_opener().open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, timeout=timeout)
    with resp:
        return json.loads(resp.read().decode("utf-8"))


def push_alerts_via_wechat(engine, new_alerts: list[dict]) -> list[dict]:
    """把 collect_new_alerts_to_push 返回的告警经微信 bridge /send 推送。"""
    if not new_alerts:
        return []
    wechat = (engine.config.get("wechat", {}) or {})
    to = wechat.get("wechat_recipient", "")
    if not to:
        print("[chiguo_daemon] --alerts-push: wechat_recipient 未配置，跳过微信推送",
              file=sys.stderr)
        return []
    loop_cfg = engine.config.get("loop", {}) or {}
    bridge_url = str(loop_cfg.get("bridge_url", "http://127.0.0.1:18790")).rstrip("/")
    token = os.environ.get("WECHAT_BRIDGE_TOKEN") or str(loop_cfg.get("bridge_token", "") or "")
    pushed: list[dict] = []
    for alert in new_alerts:
        severity = alert.get("severity", "info")
        alert_id = alert.get("alert_id", "?")
        text = f"\U0001f6a8 迟菓告警 [{severity}] {alert.get('type', 'unknown')}：{alert.get('message', '')}"
        try:
            resp = bridge_post(bridge_url, token, "/send", {"to": to, "text": text}, 10.0)
            if not resp.get("ok"):
                raise RuntimeError(str(resp.get("error") or "bridge /send ok=false"))
            alert["delivered"] = True
            pushed.append(alert)
        except Exception as e:  # noqa: BLE001 - 单条推送失败不阻断其余告警
            print(f"[chiguo_daemon] --alerts-push 推送失败 alert_id={alert_id}: {e}",
                  file=sys.stderr)
    return pushed
