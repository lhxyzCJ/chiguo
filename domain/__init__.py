"""domain — Chiguo v2 域模型层（纯函数、事件驱动、无 IO）。

Phase 4 首批：`affect`（情绪域）与 `relationship`（关系域）。
域模型不依赖 config 对象、不做文件 IO、不持有全局状态：状态以 frozen dataclass
显式进出，由 Phase 5/6 的 reducer 按事件调用并落库（storage/）。
"""
