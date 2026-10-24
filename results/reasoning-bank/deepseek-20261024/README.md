# ReasoningBank / DeepSeek：2026-10-24 完整结果

七条链全部完成：42/105，Step Accuracy 40.0%，Chain Accuracy 0/7。105 次记忆更新中，103 次写入记忆，2 次未得到可用正文并保留旧 bank 继续。

- `metrics.json`：模型、共同求解限制、官方 commit、最终指标与完整性检查。
- `task-results.csv/json`：105 个正式任务的分数、已记录用量和网络中断标记。
- `update-summaries.json`：逐题更新成功/失败和 memory item 数量。
- `comparison-all_seven.json/csv`：六方法相同 105 题的原始对照；保留 Click/CoreDNS 旧结果限制。
- `comparison-clean_five.json/csv`：去掉 Click/CoreDNS 后，六方法相同 75 题对照。
- `plots/`：五链对照的 Accuracy–Task Tokens 与 Accuracy–Total Tokens 观察点图。

所有 Token 统计包括已回传的输入与输出，失败和重试调用保留。75 条历史异常调用缺少完整 usage，未知 Token 上界 9,940,196，不补零。105 题结果和评分文件 SHA、非空 required tests 与源码指纹已核对；Loki 第 3 题的最终尝试在网络中断后仍通过私有评分，按既有共享恢复规则保留，通过与中断标记同时记录。完整私有轨迹、账本和各版本修复记录留在运行机器，不进 Git。

ReasoningBank 使用原版核心函数，统一 KIRA task solver，每题更新、Gemini embedding-001/3072 维、检索一个 experience。加入的接口适配：document input_type 映射；固定上下文内按剩余空间收小记忆输出上限；异常空正文最多一次同参数重试；单题提炼失败按官方批处理方式记录并继续。已完成任务没有因这些修复被重新抽样。

这是单次观测；尚未做相同 Total Token Budget 的 Inference Scaling 对照或重复实验。旧方法的每四题更新/最近两题支持集保持不变。方法参数并非各论文默认全量实验配置。
