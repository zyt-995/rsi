# Meta-Skill RSI 首次正式运行

运行目录：`/data2/ytzhang/rsi-harness-runs/meta-skill-20260929-qwen38-27b`

只运行新方法组，Qwen/Qwen3.8-27B；历史 baseline / skill-RSI 的 21 条完整链、103 道题均通过配置与结果有效性审计。旧实验文件未修改，新方法不读取旧 skill 或答案。

## 已完成的验收

- 项目测试：`python -m pytest -q`，137 passed、2 skipped、20 subtests passed。
- 默认 pytest 发现范围限定为 tests；第三方 benchmark 的容器内 grader 不能作为宿主项目单测导入。
- 真实 Qwen + 原 Harbor + Docker 两题合成验收，最终 attempt-03：两题均得 1 分，首题 3 次学习调用、约 55.53 秒，生成一个 meta-skill；第二题从 revision 1 实际检索到了该 skill。
- 真实 reviewer 的独立负例（negative-review-02）：拒绝了把“一次 JSON 输出验证成功”泛化为“所有文件必须是 JSON”的候选。此例不证明总体抗过度泛化能力。
- attempt-01 / attempt-02 的失败日志保留：分别暴露空 JSON、模型改写原文引号问题。最终使用 JSON schema 输出约束和宿主映射的 trace 片段编号；这些迭代只使用合成任务，没有调试性重跑正式题。
- 新版本和配置已 seal 冻结。后续不得在同一运行目录下悄悄修改算法。

## 启动时状态

- 两个独立服务：8040 跑 `break-filter-js-from-html`；8041 跑 `chess-best-move`。
- 两条链并发；链内逐题完成并更新 skill，完成后续题再换链；新链清空记忆。
- 两条首发链的全部 10 个环境已通过新预检；启动后的采样时刻 36 / 103 个环境已通过，另 2 个正在预检，未出现失败。其余继续后台预检；每条链所有环境合格后才可开跑。
- 未修改现有 GPU 绑定、模型服务或旧实验的调度状态。启动检查中旧端口占用为空，模型队列为空。
- supervisor 初始 PID：1110638；PID 可能随恢复变化，以 `status.json` / `supervisor_launch.json` 为准。

## 查看和解释

```bash
source scripts/docker-env.sh
.venv-terminal/bin/python -m rsi_harness.meta_skill_campaign \
  --root /data2/ytzhang/rsi-harness-runs/meta-skill-20260929-qwen38-27b --report
```

- `status.json`：活跃链、blocked、端口冲突和磁盘。
- `_infra/status.json`：环境预检计数与队列。
- `metrics.json`：同题三组指标、完整链指标、增益/退步案例与学习成本。
- `monitor/`：每半小时快照；这是程序留档，不是自动唤醒 Codex。
- 各链 `meta-skill-rsi/state/improvements/题名/`：原始输入、诊断、片段证据、提案、复核、前后版本及 token 用量。

正式算法收益尚无结论。不得将合成 smoke 的 1 分当作 benchmark 成绩；不做等总计算量或跨链/跨 benchmark 泛化声明。最终对比仍应等待匹配题目及完整链。

## 用户授权扩容：0–7 卡、四路并发

2026-09-29，用户要求将 GPU 4、5、6、7 也投入新方法实验。经检查，8042 / 8044 均无请求，模型、双卡并行、dtype、上下文长度等主要配置一致；LLM env 除服务端口外完全相同。103 个环境的预检均已通过。

- 新增 8042（GPU 4、5）：`bn-fit-modify`。
- 新增 8044（GPU 6、7）：`build-cython-ext`。
- 原 8040 / 8041 的 EVO-1 题目进程原样接管，未中断、未重启。
- 仅将 manifest 的 ports / concurrency 从两路改为四路；冻结算法源码、解题/学习预算、题目顺序及已完成结果均未变。
- 变更审计目录：`resource_changes/1790692353947565617/`，保留前后 manifest/seal、原活动进程与已有成绩校验和。
- 新 supervisor 初始 PID：1744161；后续以实时状态文件为准。8、9 卡未动。
