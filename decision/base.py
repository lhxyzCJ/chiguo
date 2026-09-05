"""decision.base — DecisionEngine 基础设施（构造/基座锚定/配置热重载/快照）。

拆自 chiguo_daemon.py：本模块持有引擎的构造与运行时锚定骨架，
业务决策/上下文/记账/loop 发送等责任分别落在 decision.core / decision.context /
ops.engine_ops / runner.loop 四个 mixin，由 decision.engine 组合成完整 DecisionEngine。
"""
import os
import sys
import tomllib
from datetime import datetime
from chiguo_time import CST
from pathlib import Path

from chiguo_state import ChiguoState
from chiguo_topics import TopicPicker
from chiguo_composer import MessageComposer
from chiguo_paths import PROJECT_ROOT
from schedule.facade import ScheduleFacade, PlayProofProvider



class DecisionEngineBase:
        def __init__(self, config_path: str = None,
                     log_path: str = None):
            # v8: 默认 config 锚定到项目根（拆包后 __file__ 指向子目录，用统一 PROJECT_ROOT）
            if config_path is None:
                config_path = str(PROJECT_ROOT / "chiguo_proactive.toml")
            self.config_path = config_path
            with open(config_path, "rb") as f:
                self.config = tomllib.load(f)
            self._merge_experimental()
            # ── v6: 路径锚定。所有运行时文件基于 config 所在目录，
            # 不依赖 cwd（cron 工作目录漂移曾导致状态文件反复丢失重建）。
            self._inject_base_dir()
            self._config_mtime = os.path.getmtime(config_path)
            self.state = ChiguoState(self.config)
            # PR-3: 日程门面 + 播放反证提供者
            self.schedule_facade = ScheduleFacade(str(self._base_dir), self.config)
            self._play_proof_provider = PlayProofProvider(self.config, str(self._base_dir))
            self.netease_service = self._play_proof_provider.service
            # 显式 log_path（测试）原样使用；否则锚定 base_dir
            # （先于 topic_picker 构造：A9 recent_sent_texts() 依赖 messages_log_path）
            self.log_path = log_path or str(self._base_dir / "chiguo_decisions.jsonl")
            self.messages_log_path = self._base_dir / "chiguo_messages.jsonl"
            self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                            netease_service=self.netease_service,
                                            recent_sent_texts=self.recent_sent_texts())
            self.composer = MessageComposer(self.state, self.config.get("composer", {}), schedule_facade=self.schedule_facade)
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
            except (ValueError, TypeError, OSError):
                pass  # rotation failure never blocks daemon

        def _merge_experimental(self):
            """把 [experimental] 归档键按 `section__key` 合并回对应主段（灰度覆盖）。

            归档前主段删键 + experimental 存 `section__key`；此处按 `__` 切分回填，
            未声明段落则懒创建。重载与首次加载共用，保证 279→218 行为恒等。
            """
            exp = self.config.get("experimental")
            if not isinstance(exp, dict) or not exp:
                return
            for k, v in list(exp.items()):
                if "__" not in k:
                    continue
                sec, key = k.split("__", 1)
                if not sec or not key:
                    continue
                self.config.setdefault(sec, {})[key] = v

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
                self._merge_experimental()
                self._inject_base_dir()
                self._config_mtime = mtime
                # ── Q19: 热重载重建集合补全。ChiguoState.reload_config() 替换 config 引用并
                # 重建 config 派生组件:personality 初始基线 + holiday_parser + cooldown 静默窗口
                # (置信度达标用学习窗口,否则回退新 config 默认)。runtime 持久化状态不动。──
                self.state.reload_config(self.config)
                self.schedule_facade.reload(self.config)
                self._play_proof_provider = PlayProofProvider(self.config, str(self._base_dir))
                self.netease_service = self._play_proof_provider.service
                try:
                    self.composer.schedule_facade = self.schedule_facade
                except Exception:
                    pass
                self._schedule_facade = self.schedule_facade
                self.topic_picker = TopicPicker(self.state, self.config.get("topic_picker", {}),
                                                netease_service=self.netease_service,
                                                recent_sent_texts=self.recent_sent_texts())
                self.composer = MessageComposer(self.state, self.config.get("composer", {}), schedule_facade=self.schedule_facade)
                self.state._bayesian_estimator = None

        def snapshot(self):
            return self.state.snapshot(datetime.now(CST))

