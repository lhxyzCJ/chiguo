# 内部部署速查（本机运维）

> 完整项目文档见根目录 [README.md](../README.md)；系统文档见 [doc/SYSTEM.md](SYSTEM.md)。

```bash
uv run python chiguo_daemon.py --stats --alerts --monitor   # 系统状态
uv run python chiguo_daemon.py --stats --monitor           # 系统状态（stats+alerts+health）
uv run python chiguo_envcheck.py                            # 环境就绪检查
bash scripts/wechat-bridge.sh status                        # 微信桥状态
uv run python -m schedule.replan --check   # 重分析检查（来源变化时重写 plan）
uv run python -m schedule.holiday                           # 节假日查询
tail -f logs/cron-tick.log                                  # 主动发送日志
```

完整 CLI 参考见 [doc/SYSTEM.md](SYSTEM.md) 七、CLI 参考；配置参考见九、配置参考。

---

**v1.10 变更点速查**（外部对比优化 9 项，详见 [doc/SYSTEM.md](SYSTEM.md)）：

| 变更 | 章节 |
|------|------|
| A1 弹性衰减（effective_hl = half_life/(1+\|gap\|/baseline)）+ A2 情绪交互矩阵 | §2.3 |
| A10 回复饱和阻尼（30 分钟窗口 ×0.5^min(n,3)） | §2.4 |
| A3 日程乘数×抖动 / A4 三段激活 / A5 未回复退场状态机 / A6 repeat 阻尼泛化 | §2.6 |
| A9 内容级防复读（3-gram Jaccard 弃用候选） | §4.1 |
| A8 生成失败确定性回退（chiguo_composer 兜底 CLI + tick 接入） | §5.7、七、CLI 参考 |

版本历史见 §十六（v1.10，2026-08-09）。
