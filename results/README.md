# 导出的实验记录

DeepSeek 七链原始记录和五链筛选汇总的使用边界见 `docs/handover.md`。

- `deepseek/metrics.json` 是按唯一调用 ID 与历史费用账本核对的汇总，含输入/输出、RSI 阶段拆分、缺失 usage 上界。
- `task-results.json` / CSV 是 525 条原始正式 task summary 的白名单字段；row usage 是当时记录值，后续账单核对以 metrics.json 为准，不混用两者直接相加。
- `update-summaries.json` 是 84 次更新的白名单字段，保留部署/接受标志与 Skill hash；没有导出完整提示词、隐藏测试或学习文本。不同方法缺少某个字段并不代表该量为零。
- `plots/` 是这次运行的散点观察，不是预算扫描曲线；相应限制保存在 observations.json。
- `audited-five-chain-summary.json` 是排除 Click/CoreDNS 的现有五链统计，不表示做过重复试验或等总预算对照。

GPT 的不完整进展在交接文档记录，未与 DS 的完整链分数放到同一结果表。
