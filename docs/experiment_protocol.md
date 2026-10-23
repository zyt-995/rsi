# 实验协议

## 目标与队列

研究动态代码环境中链内适应是否带来足够收益，抵消自改进成本。固定数据版本和 105 题顺序见 `configs/benchmark/seven-chains.json`。七个仓库为 Click、CoreDNS、NATS、Fiber、Loki、Istio、Prometheus，每条链 15 个有序节点。

每组独立按 0–14 运行，组间并行。每个任务使用 Benchmark 提供的当前版本快照，不把上一题 agent 的补丁直接搬到下一题。链内只继承方法定义的笔记、Skill 或 Harness；新链清空学习状态。所有组共享相同的公开历史接口，可检索当前仓库。Static 因此不是失去仓库历史的空白模型，但没有额外学习状态。

## 固定求解设置

| 参数 | DeepSeek | GPT |
|---|---|---|
| 路由 | OpenRouter / deepinfra/fp8 | OpenRouter / openai |
| 模型 | deepseek/deepseek-v4.1-flash | openai/gpt-6.1-sol |
| reasoning | high | high |
| temperature / top_p | 1 / 0.95 | 不发送 |
| 单次 context / output | 262144 / 65536 | 262144 / 65536 |
| 单题累计 Token 上限 | 无 | 无 |
| 最大 Agent 轮数 | 100 | 100 |
| 求解 / 评分超时 | 7200s / 600s | 7200s / 600s |

配置以 JSON 为准。GPT 的 o200k_base 只用于保守准入和笔记长度估计，明确不宣称等同模型官方 tokenizer；最终统计优先使用 provider usage。一个 session 的费用账本由五组共享，默认配置上限 50 美元是运行保护阈值，并非每组相同 Token Budget。多个 session 默认有独立账本，不能将其误认为跨链共享的 50 美元上限。

## 学习与隔离

在完成第 4、8、12 题后更新；以最近两道已完成题为支持集，最后一题之后不更新。只允许当时可见的任务说明、公开执行轨迹和评测反馈；隐藏测试与参考补丁只在评分侧使用，meta sandbox 不挂载私有数据。更新后结果只用于后续正式题，不能回填之前的分数。

每个方法的初始求解器是同一个上游 KIRA。Meta-Harness 修改后可以改变 Harness；EvoSkill 修改 Skill；简单方法修改笔记。这些差异是实验变量。统一环境、模型、任务次序、求解上限和计费规则，不意味着各方法的 RSI 工作量相同。

## 指标与成本

- Step Accuracy：正式通过数 / 正式已完成数。未完成和基础设施异常单独报告，不自动当作算法失败。
- Chain Accuracy：整条链全部正式通过的完整链数 / 完整链数。
- Task Tokens：正式求解的输入和输出。
- RSI Tokens：分析、修改、额外 rollout、候选验证和 frontier 刷新全部输入和输出。
- Total Tokens = Task + RSI；缓存输入仍属于输入 Token，费用按供应商实际账单统计。
- 调用 ID 去重；保留失败和重试费用。usage 缺失不得填成零或从上界冒充准确值，报告 unknown 数和保守上界。

输出单次运行点不等于完整 Accuracy–Compute 曲线。要支持“相同预算优于 inference scaling”，仍需要多个预算点、对应推理扩展对照和重复实验。当前结果只支持已运行配置的描述性比较。
