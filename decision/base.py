"""decision.base — DecisionEngine 基础设施（构造/基座锚定/配置热重载/快照）。

拆自 chiguo_daemon.py：本模块持有引擎的构造与运行时锚定骨架，
业务决策/上下文/记账/loop 发送等责任分别落在 decision.core / decision.context /
ops.engine_ops / runner.loop 四个 mixin，由 decision.engine 组合成完整 DecisionEngine。
"""
import os
import sys
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

from chiguo_state import ChiguoState
from chiguo_topics import TopicPicker
from netease.service import NeteaseService
from chiguo_composer import MessageComposer
from chiguo_paths import PROJECT_ROOT

CST = timezone(timedelta(hours=8))


class DecisionEngineBase:
        def __init__(self, config_path: str = None,
                     log_path: str = None):
            # v8: 默认 config 锚定到项目根（拆包后 __file__ 指向子目录，用统一 PROJECT_ROOT）
            if config_path is None:
                config_path = str(PROJECT_ROOT / "chiguo_proactive.toml")
            self.config_path = config_path
            with open(config_path, "rb") as f:
                self.config = tomllib.load(f)
            # ── v6: 路径锚定。所有运行时文件基于 config 所在目录，
            # 不依赖 cwd（cron 工作目录漂移曾导致状态文件反复丢失重建）。
            self._inject_base_dir()
            self._config_mtime = os.path.getmtime(config_path)
            self.state = ChiguoState(self.config)
            # v9: 网易云策略层(健康/配额/音乐话题),base_dir 锚定
            self.netease_service = NeteaseService(self.config, str(self._base_dir))
            # 显式 log_path（测试）原样使用；否则锚定 base_dir
            # （先于 topic_picker 构造：A9 recent_sent_texts() 依赖 messages_log_path）
            self.log_path = log_path or str(self._base_dir / "chiguo_decisions.jsonl")
            self.messages_log_path = self._base_dir / "chiguo_messages.jsonl"
            self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                            netease_service=self.netease_service,
                                            recent_sent_texts=self.recent_sent_texts())
            self.composer = MessageComposer(self.state, self.config.get("composer", {}))
            print(f"[chiguo_daemon] base_dir={self._base_dir}", file=sys.stderr)
            print(f"[chiguo_daemon] state={self.state.state_path}", file=sys.stderr)
            print(f"[chiguo_daemon] log={self.log_path}", file=sys.stderr)
    
    
            # ── v5: monotonic 锚点（不持久化，用于检测壁钟跳变）──
            self._monotonic_at_save: float = 0.0
    
            # ── v5: 日志轮转（每次进程启动检查一次）──
            try:
                from chiguo_rotation import rotate_if_needed
                rotate_if_needed(
                    [str(self.log_path), str(self.messages_log_path)],
                    self.config_path,
                )
            except Exception:
                pass  # rotation failure never blocks daemon

        def _inject_base_dir(self):
            """config 中注入 _base_dir（ChiguoState 用它锚定运行时路径）。"""
            self._base_dir = Path(self.config_path).resolve().parent
            self.config["_base_dir"] = str(self._base_dir)

        def _maybe_reload_config(self):
            """检测 toml 文件 mtime，变化时热更新配置（--loop 模式用）"""
            try:
                mtime = os.path.getmtime(self.config_path)
            except OSError:
                return
            if mtime > self._config_mtime:
                try:
                    with open(self.config_path, "rb") as f:
                        new_config = tomllib.load(f)
                except (OSError, ValueError) as e:
                    # TOML 语法错误/读取失败 → 保留旧配置，打 stderr 告警继续运行
                    print(f"[warn] 配置热重载失败，保留旧配置: {e}", file=sys.stderr)
                    return
                self.config = new_config
                self._inject_base_dir()
                self._config_mtime = mtime
                self.state.config = self.config
                # ── v7: 热重载后同步生物钟窗口(置信度达标用学习窗口,否则回退配置默认,
                # 否则 cooldown 内注入的窗口保持旧值,silent_hours/逃生阀判定陈旧)──
                self.state._sync_quiet_window()
                # v9: 热重载时同步重建策略层(重试/配额参数可能被改)与 TopicPicker
                self.netease_service = NeteaseService(self.config, str(self._base_dir))
                self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                                netease_service=self.netease_service,
                                                recent_sent_texts=self.recent_sent_texts())
                self.composer = MessageComposer(self.state, self.config.get("composer", {}))
                self.state._bayesian_estimator = None

        def snapshot(self):
            return self.state.snapshot(datetime.now(CST))

