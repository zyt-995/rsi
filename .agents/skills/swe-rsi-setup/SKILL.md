---
name: swe-rsi-setup
description: 配置本仓库 SWE-Chain-Evo RSI 实验的固定依赖、模型无关环境和隔离评分，检查新机器是否具备复现实验条件。用于安装、迁移和环境故障排查。
---

# SWE RSI 环境配置

在仓库根目录操作。先读 `docs/reproduction.md`，涉及实验参数时读 `docs/experiment_protocol.md` 和 `docs/methods.md`；接手既有成绩时读 `docs/handover.md`。脚本统一位于仓库 `scripts/`，不需要复制一份到 skill。

## 工作流程

1. 检查 Python 3.12、系统工具、workspace 和 Docker data-root 的空余空间。使用用户现有 Docker daemon，保留已有网络设置；`RSI_WORKSPACE` 与 Docker 存储是两个不同位置。
2. 根据 `.env.example` 配置本机路径。密钥只保存在本地环境变量或被忽略的 `.env`，不输出密钥；环境准备不需要密钥。
3. 执行 `bash scripts/setup.sh`，激活 `.venv` 后运行 `python scripts/doctor.py --docker`。固定子模块 commit 和 Harbor 补丁由 bootstrap 校验；不要改装最新 upstream main 来绕过版本错误。
4. 运行 `python -m pytest tests`。记录跳过项；单元测试通过不等同于真实容器和 meta sandbox 都可用。若 user namespace 不可用，修复主机条件，不能删除 bwrap 隔离继续实验。
5. 执行 `python scripts/fetch_data.py`；先运行所选链的 `prepare.py --chain NAME --plan`，核对压缩包和 Docker 展开空间，再准备选定任务。默认先用 Fiber `--indices 0 --environment-only` 校准一题；按用户请求扩展完整链。
6. 参考补丁应为 1、空提交应为 0；完整链还需要实际 Harbor Noop probe、task SHA 和 source pins 全部通过。失败时保留日志并修复环境，不能手写 passed 来绕过。
7. 汇报环境验证结果、未完成项、运行目录和后续启动命令。仅请求配置环境不包括启动付费实验；用户已要求启动时，沿用其模型/预算授权，通过 `scripts/run.py start` 启动，不重复索要确认。

## 重要约束

- 当前入口是五组共享 KIRA 初始 Harness、链内继承、链间清空，4/8/12 更新、最近两题支持集。迁移环境不得悄悄修改这些参数。
- ModularRSI 仅保留旧适配，尚未纳入当前五组启动入口。
- 不把私有评分测试、参考补丁、账本凭据挂入求解器或 meta sandbox。不能以可复现为理由上传整个 `.work`。
- 开始新链使用新的 session；并行 session 需要互不冲突的四个连续端口。默认费用账本按 session 独立，不是所有链共享总额度。
- source pins 或模型 usage 不一致时先核对具体记录。保留中断、重试和未知账单；不要删除旧结果后当成没有发生过。
