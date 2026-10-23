# 方法与原版的关系

上游 commit 见 `upstream/manifest.json`。官方仓库通过 Git 子模块固定；方法参数见 `configs/methods/`。

| 方法 | 可继承状态 | 每次更新 |
|---|---|---|
| Static | 无额外状态 | 不更新 |
| Experience Notes | 最多 2048 token 笔记 | 最近两题的公开轨迹与旧笔记，单次生成；无额外求解验证 |
| Failure-only Notes | 最多 2048 token 笔记 | 只分析窗口内失败题；全部成功则跳过 |
| Meta-Harness | Python Harness / incumbent | 原生 run_evolve / update_frontier；1 search iteration，incumbent 与新候选在最近两题各跑一次（同一候选的首个验证可复用其相同输入 smoke，调用 ID 只计一次） |
| EvoSkill | .claude/skills 与原生 frontier | 原生 SelfImprovingLoop / ProgramManager；1 iteration，1 category × 2 samples，frontier_size=3，best selection |

两种复杂方法均每四题更新、最近两题验证、每题一个验证 attempt；这些是**本项目的轻量在线参数，不是论文默认全量配置，也不称为官方 smoke**。EvoSkill 会刷新存活 frontier 在新窗口上的成绩，因而一次 iteration 仍可能包含多次完整解题，RSI Tokens 不能只数 proposer。

EvoSkill 的 proposer/generator 使用官方 Claude SDK 运行结构，经协议桥接调用同一个实验模型。使用 Claude Code 作为工具运行时并不意味着调用 Claude 模型。Meta-Harness 同样保留官方搜索与筛选操作，通过本项目的 SWE adapter 接入任务、账本和沙箱。

适配包含：在线支持集选择、相同 KIRA task agent、公开历史与评分接口、无网络求解环境、模型网关、source pins、隔离评分、Skill discovery 校验、跨窗口 resume marker 处理、长轨迹缩减视图。完整原始轨迹保留。不得写成“完全没有改动的论文复现”。

ModularRSI 的原有接入代码仍位于 `src/methods/modular_update.py` 等模块，官方 commit 也固定；当前便携入口只启动以上五组。若恢复 ModularRSI，先单独定义与当前 KIRA 五组的比较协议与配置，不把其旧 Terminal 结果混入七链表。
