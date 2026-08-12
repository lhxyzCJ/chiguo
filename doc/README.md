# 内部部署速查（本机运维）

> 完整项目文档见根目录 [README.md](../README.md)；系统文档见 [doc/SYSTEM.md](SYSTEM.md)；
> agent/LLM 集成见 [doc/AGENT_INTEGRATION.md](AGENT_INTEGRATION.md)（唯一权威文档）；
> 微信命令见 [doc/微信命令.md](微信命令.md)。

```bash
uv run python chiguo_daemon.py --stats --alerts --monitor   # 系统状态（stats+alerts+health）
uv run python chiguo_envcheck.py                            # 环境就绪检查
bash scripts/wechat-bridge.sh status                        # 微信桥状态
uv run python -m schedule.replan --check                    # 重分析检查（来源变化时重写 plan）
uv run python -m schedule.holiday                           # 节假日查询
tail -f logs/cron-tick.log                                  # 主动发送日志
```

完整 CLI 参考见 [doc/SYSTEM.md](SYSTEM.md) 七、CLI 参考；配置参考见九、配置参考；agent/LLM 链路速查见
[doc/AGENT_INTEGRATION.md](AGENT_INTEGRATION.md) 十二、维护速查。
