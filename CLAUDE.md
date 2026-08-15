# CLAUDE.md
## 工程原则
1. 不保留向后兼容，过时路径直接删，别加兼容层
2. 选满足当前需求的最简实现，别搞多余抽象配置
3. 先跑通最小可用版本，再叠新功能，别换现有能跑的代码
4. 组件模块化，业务关注点分离
5. 优先用成熟维护的第三方库，没理由别重复造轮子
6. 先查现有项目依赖的能力，再考虑加包或自研
7. 架构决策看长期，别用临时凑合用的过渡方案
8. 参考成熟产品的验证方案，别从零发明

## 测试
- 全量测试：`bash scripts/ci-test.sh`（py 走 pytest 收集，mjs/sh 脚本链保留；计数动态化以 ci-test.sh 为准，任一失败退出非零，与 GitHub Actions 同一入口）
- 单文件：`uv run pytest tests/test_*.py -q`（pytest）；py 测试无 runner 脚手架，全局隔离见 tests/conftest.py

## Git 工作流（代码改动）
- 代码改动（refactor:/fix:/feat:）一律走分支 + PR：分支名 需按照具体问题自定；每分支对应一个 GitHub Issue（`gh issue create`），PR 正文 `Closes #N`；出口条件 = CI 全链绿 + 子代理自审 + 用户批准 → squash merge。
- docs:/chore: 小改动可直接 main（CI 仍自动验证）。
- 简化单元跟踪：从审查报告（~/chiguo-meta/audit/）拆出的每单元建 Issue，标题 根据问题自定，正文含文件清单、改动内容、删行预估、依赖单元。
每次修改代码后必须：


每次代码更新需审计相关文档中受影响的章节
