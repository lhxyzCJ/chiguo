# 内部部署速查（本机运维）

> 完整项目文档见根目录 [README.md](../README.md)；系统文档见 [doc/SYSTEM.md](SYSTEM.md)。

```bash
uv run python chiguo_daemon.py --stats --alerts --monitor   # 系统状态
uv run python chiguo_monitor.py --summary --health          # 健康摘要
uv run python chiguo_watchdog.py --quiet                    # 看门狗（退出码驱动）
uv run python chiguo_envcheck.py                            # 环境就绪检查
bash scripts/wechat-bridge.sh status                        # 微信桥状态
tail -f logs/cron-tick.log                                  # 主动发送日志
```

完整 CLI 参考与配置说明见根 README「CLI 参考 / 配置」节。
