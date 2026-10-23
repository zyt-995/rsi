# Token-Efficient RSI Benchmark

在持续变化的软件仓库中，比较复杂 RSI 与简单记忆的收益和完整计算成本。
主评测为 **SWE-Chain-Evo 的 7 条 15 步链**；每条链内继承经验，链之间重置。

当前五组：Static、经验笔记、失败笔记、Meta-Harness、EvoSkill。它们均以官方 KIRA 为初始解题 Harness。
ModularRSI 的既有适配代码与固定上游版本一并保留，但不属于当前五组的 105 题汇总。

## 快速开始

Linux x86_64、Python 3.12.14、固定版本 Docker/Compose、bubblewrap、Go 1.25.0、uv 和原生 Claude Code 2.1.299。
完整工具版本与源码指纹见 `upstream/reproduction-reference.json`；启动前强制核对一致性。
详细安装与存储说明见 [环境与复现](docs/reproduction.md)。

```bash
git clone --recurse-submodules git@github.com:zyt-995/rsi.git
cd rsi
cp .env.example .env
# 编辑本地 .env，设置工作目录、网络选项；准备环境时不需要 API key。
source .env
bash scripts/setup.sh
source .venv/bin/activate
python scripts/bootstrap.py
python scripts/doctor.py --docker
python scripts/check_reproduction.py
python scripts/fetch_data.py
python scripts/prepare.py --chain fiber --plan
python scripts/prepare.py --chain fiber --model deepseek --budget-usd 50
# 以下命令才启动付费实验。先在 shell/.env 设置 OPENROUTER_API_KEY。
python scripts/run.py start --session "${RSI_WORKSPACE:-$PWD/.work}/sessions/deepseek-fiber"
```

准备流程会校验固定数据 SHA、加载官方镜像、创建隔离的求解快照、执行参考解/空提交评分检查，
并运行零模型 Harbor 检查。只有完整 15 题链准备成功后才能启动正式实验。
GPU 不需要；模型通过 API 调用。每组按题目顺序运行，组间并行。

使用 AI 助手安装环境时，调用仓库 skill：

> 使用 `.agents/skills/swe-rsi-setup/SKILL.md` 帮我配置环境，并验证 Fiber 第一题；暂不调用付费模型。

## 代码布局

| 路径 | 内容 |
|---|---|
| `src/benchmark/swe_chain_evo/` | 数据、镜像、任务、隔离评分适配 |
| `src/agents/within_chain/` | 简单记忆与链内运行编排 |
| `src/agents/evoskill_swe/` | 官方 EvoSkill 的 Benchmark 接口 |
| `src/runner/formal_swe_chain/` | Static/Meta-Harness 运行、验证与报告 |
| `src/runner/rsi_portable/` | 跨机器路径与配置生成 |
| `src/methods/` | 保留原有导入名的原生方法适配、网关和账本 |
| `src/accounting/` | 费用账本的公共导出接口 |
| `configs/` | DS/GPT、方法参数、105 题顺序及镜像清单 |
| `upstream/` | 固定 Git 子模块、Harbor 补丁、来源记录 |
| `results/` | 已有汇总、逐题成绩、更新摘要与图表 |
| `docs/` | 协议、方法差异、复现和交接说明 |

为保留原方法逻辑，内部模块名继续使用 `methods`、`within_chain` 等；目录重组主要作用于安装与路径配置。
`scripts/bootstrap.py` 会在可配置的 `.work` 下创建运行布局，本机绝对路径不写入 Git。

## 现有结果与边界

DS 已执行 525 次：5 方法 × 7 链 × 15 题。七链原始正确率：Static 43.8%、经验笔记 41.9%、
失败笔记 48.6%、Meta-Harness 45.7%、EvoSkill 41.0%。
**Click 仍需旧批次等价性审计；CoreDNS 旧 EvoSkill 有 Skill 加载问题，修复后的完整重跑尚未完成。**
`results/deepseek/metrics.json` 明确记录这些限制，另附不含这两条链的五链汇总。
GPT 实验只完成少量题，尚未开始首次 RSI 更新；参见 [交接说明](docs/handover.md)。

```bash
python scripts/run.py status --session /your/session
python scripts/run.py pause --session /your/session
python scripts/analyze.py --input results/deepseek/metrics.json --output .work/plots
python -m pytest tests
```

这些是单次运行的观察结果，未实施相同 Total Token Budget 的 Inference Scaling 对照。
后续应同时报告解题、自改进以及失败重试费用，避免只比较最终 Harness 的执行成本。

上游许可见各子模块的 LICENSE；本仓库的第三方修改记录见 [依赖与修改](docs/upstream.md)。
