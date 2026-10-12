# 迟菓项目版本号 —— 唯一来源是 pyproject.toml 的 [project].version
#
# 版本格式：MAJOR.MINOR 字符串（如 "1.10"）。版本号是标识符，不是数值——
# 禁止按十进制加法解读（1.9 + 0.1 = 2.0 是错误推导）。
#
# 步进规则（维护者手动，每完成一轮功能修改执行一次）：
#   - MINOR + 1：1.9 → 1.10 → 1.11 → 1.12 → 1.13 → 1.14 …（次版本计数递增，类比 Linux 内核 6.9 → 6.10）
#   - MAJOR 保持 1；仅在重大架构变更或维护者明确决定时 +1（届时 1.xx → 2.0）
#
# 改版本时只改 pyproject.toml 一处，然后跑 `uv lock` 重新派生 uv.lock
# （`uv lock --check` 可验证 uv.lock 是否已漂移）。本模块运行时读取，不手改。
# 暴露点：`chiguo --version` / `db status` JSON / `serve` 启动输出。
import tomllib
from pathlib import Path

VERSION = tomllib.loads(
    (Path(__file__).resolve().parent / "pyproject.toml").read_text(encoding="utf-8")
)["project"]["version"]
