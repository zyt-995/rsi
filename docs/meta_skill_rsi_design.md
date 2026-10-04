# Evidence-grounded Meta-Skill RSI v1

2026-09-29：只新增方法组，历史 baseline、skill-RSI 保持只读。

## 方法与边界

每条链从空 skill 库开始；每道有效计分题结束后（链末题除外），执行：

1. Diagnose：从本题自己的公开执行记录中提取成功、失败、修复证据；模型选择原始窗口中的片段编号，宿主据此保留原文、字符位置、观察/假设、已修复/未修复/未知。每片段 480 字符，拼接后严格等于旧组可见的同一 trace 窗口；不会额外读历史。最终 0 分不能单独证明哪个错误导致失败。
2. Propose：新增或修改最多 3 个 skill。task skill 和 meta skill 共用一个库。meta skill 是根据当前任务推导约束、检查适用条件的程序性规则，不是无条件类推。
3. Review：同一冻结模型重新对照原始执行记录，检查因果解释、适用范围、反例和旧规则冲突；拒绝有未支持论断的候选。无可接受更新时保留旧库。

宿主只做结构校验、原文引用校验、配额校验和版本提交。模型对因果关系的判断仍可能错误；原文存在不等于解释正确。LLM review 不是执行验证、held-out regression 或跨任务泛化证明。

每个 skill 包含 When、Mechanism、Steps、Cautions、尚未执行的 Transfer check；溯源存放在独立审计文件里。任何未来验证建议都必须由原解题器在原解题预算内决定是否执行。不会额外重放正式题来筛选 skill，不更新 harness、模型权重或 updater 本身。

## 冻结配置

| 项目 | 配置 |
| --- | --- |
| 数据集 | Terminal-Bench-Evo，旧两组均整链完成的 21 链 / 103 题 |
| 模型 | Qwen/Qwen3.8-27B，冻结，与旧组相同 |
| 原解题器 | 原有 CleanTerminalAgent 的 skill-rsi 加载接口；外层方法标签为 meta-skill-rsi |
| 解题 | 15 轮、最多 15 次逻辑模型调用、120 条终端命令；每次输出上限 32768 tokens |
| 时间 | agent 7200 秒；官方 verifier 600 秒 |
| 更新 | 每题最多 3 次调用，输出上限分别 2048 / 4096 / 2048；整个更新共 600 秒 |
| 更新采样 | temperature=0.2，关闭 thinking，不补问、不重采样 |
| 可见信息 | 已完成题的 instruction 前 8000 字符、标量分数、与旧组完全相同的公开 trace 尾部最多 24000 字符、当前 skill 库 |
| 不可见信息 | 隐藏测试、grader stdout、未来题目、oracle、旧实验答案和 skill |
| 记忆配额 | task/meta 合计最多 16 个文档、32768 JSON bytes |
| 检索 | 沿用原词项重叠排序，top 4、12000 字符，不加另一条注入通道 |
| 状态范围 | 链内逐题更新；链间清空；共最多 82 次更新机会 / 246 次学习调用 |
| 调度 | 当前四条链并发、链内串行，优先完成已开始的链；8040、8041、8042、8044；不重配 GPU 服务。初始为两路，2026-09-29 经用户授权仅扩展资源 |

学习成本高于旧 skill-RSI 的一次更新调用，不能宣称等总计算预算。该 21 链是已观察的开发/配对样本，不是未接触的最终盲测集；本阶段只检验链内迁移。

## 隔离和有效性

- 新运行目录：`/data2/ytzhang/rsi-harness-runs/meta-skill-20260929-qwen38-27b`。日志置于 data2，不挤占余量较小的 home 分区。
- `manifest.json` 冻结任务顺序、数据/环境校验和、协议和历史对照审计摘要；真实合成 smoke 通过后 `seal.json` 冻结代码与验收摘要。
- 旧 206 个对照记录全部先核对模型、轮数/调用数、命令数、timeout、task checksum 和 verifier 有效性。每次新题启动前再检查对应对照摘要。
- 103 个环境独立重做无模型 smoke：镜像、tmux、verifier 是否可用。预检在空白环境上测试失败是预期的，不算算法分数。每条新链所有环境通过后才入场。
- 每题新建原 benchmark 环境；传递的只有 skill 库，不传上题容器文件系统。
- 环境错误隔离为 blocked，不计 0、不继续越过链中间题；有效 0 分正常计入，不重跑。
- 有效评分先写入 `scored.json`，然后学习；异常中断不能自动重做模型请求或试题。
- 历史服务若在目标端口工作，或服务请求队列非空，调度器不新派任务。旧调度器不识别本实验的锁，因此仍应保持旧队列无可运行工作，并监控端口冲突；本实现不会修改旧 PAUSE/状态文件。

## 审计与指标

每次更新保存 source、三阶段 request/response、原文引用位置、候选与审查理由、before/after 和用量。请求日志不含 API 密钥。

`metrics.json` 每个调度周期更新：

- 完全相同题目上的三组分数、提升/退步题目。
- 完整链匹配结果，以及链均值再平均；不把部分链当完整链。
- 新方法解题/学习调用、token、耗时，更新接受数；未知用量不能当作实测 0。
- 环境异常单列。部分链结果属于描述性中间结果，不用于提前挑选算法版本。

调度器约每 20 秒刷新状态，每半小时保存一次 monitor 快照。这是后台程序检查和留档，不等同于每半小时唤醒 Codex 会话。

运行方式（均先在项目目录 `source scripts/docker-env.sh`）：

```bash
.venv-terminal/bin/python -m rsi_harness.meta_skill_campaign --root RUN --prepare
.venv-terminal/bin/python -m rsi_harness.meta_skill_campaign --root RUN --preflight
.venv-terminal/bin/python scripts/accept-meta-skill.py --root ACCEPTANCE --port 8040
.venv-terminal/bin/python -m rsi_harness.meta_skill_campaign --root RUN --seal --acceptance ACCEPTANCE
.venv-terminal/bin/python -m rsi_harness.meta_skill_campaign --root RUN --run
.venv-terminal/bin/python -m rsi_harness.meta_skill_campaign --root RUN --report
```

正式运行不修改冻结源文件；算法协议变化必须另开版本目录。经用户授权的纯资源扩容可通过 `scripts/resize-meta-skill-workers.py` 留档：只改 ports/concurrency，保存前后 manifest/seal，接管调度器但不中断任务。创建新运行目录的 `PAUSE` 可停止派新题，当前题正常收尾。进程中断、来源变更或未知基础设施故障必须先排查，不能盲目删记录重跑。
