# ============================================================
# chiguo_sender.py — 消息发送器
# Demo模式：打印到控制台
# 生产模式：调用 OpenClaw WeChat 内部 API
# ============================================================

import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path


class MessageSender:
    """消息发送接口"""

    def __init__(self, config: dict):
        self.config = config
        self.wechat_channel = config.get("openclaw", {}).get("wechat_channel", "openclaw-weixin")
        self.wechat_recipient = config.get("openclaw", {}).get("wechat_recipient", "")
        self.log_path = Path("chiguo_message_log.json")
        self.history: list[dict] = self._load_log()

    def send(self, message: str, trigger_type: str, sim_time: datetime = None) -> bool:
        """
        发送消息。
        返回 True/False。
        """
        now = sim_time or datetime.now()

        record = {
            "time": now.strftime("%Y-%m-%d %H:%M"),
            "type": trigger_type,
            "message": message,
            "sent": False,
        }

        # 尝试生产发送
        success = self._send_via_openclaw(message)
        record["sent"] = success

        # 始终打印（demo可见）
        outbox_exists = (Path.home() / ".openclaw" / "outbox").exists()
        if success:
            status = "✅ 已发送"
        elif outbox_exists:
            status = "❌ 发送失败"
        else:
            status = "🖨 模拟发送"
        print(f"\n{'='*55}")
        print(f"💬 迟菓主动消息 [{record['time']}]")
        print(f"   触发: {trigger_type}")
        print(f"   {status}: {message}")
        print(f"{'='*55}\n")

        self.history.append(record)
        self._save_log()
        return success

    def _send_via_openclaw(self, message: str) -> bool:
        """
        通过 OpenClaw 内部 API 发送微信消息。
        注意：此 API 可能因 OpenClaw 版本而异，需要根据实际情况调整。
        当前为占位实现——对接 OpenClaw 的 weixin channel adapter。
        """
        try:
            # OpenClaw 的微信通道通常通过内部 gateway IPC 或 HTTP API
            # 实际端点需要确认。以下是几种可能的路径：
            #
            # 方案A：如果 OpenClaw 暴露了内部 HTTP API
            # POST http://localhost:<gateway_port>/api/channels/send
            #
            # 方案B：通过 clawbot 微信 bridge
            # POST http://localhost:8888/send
            #
            # 方案C：写入 OpenClaw 的 outbox 目录
            # echo '{"channel":"openclaw-weixin","to":"...","msg":"..."}' > ~/.openclaw/outbox/

            outbox_dir = Path.home() / ".openclaw" / "outbox"
            if outbox_dir.exists():
                msg_file = outbox_dir / f"proactive_{datetime.now().timestamp():.6f}.json"
                msg_file.write_text(json.dumps({
                    "channel": self.wechat_channel,
                    "recipientId": self.wechat_recipient,
                    "message": message,
                    "source": "chiguo_proactive",
                }, ensure_ascii=False))
                return True

            return False  # Demo模式：不尝试发送
        except Exception as e:
            print(f"  ⚠ 发送失败: {e}")
            return False

    def _load_log(self) -> list[dict]:
        if self.log_path.exists():
            data = json.loads(self.log_path.read_text())
            if isinstance(data, list):
                return data
        return []

    def _save_log(self):
        # 只保留最近200条
        recent = self.history[-200:]
        data = json.dumps(recent, indent=2, ensure_ascii=False)
        tmp = Path(str(self.log_path) + ".tmp")
        tmp.write_text(data)
        os.replace(tmp, self.log_path)
