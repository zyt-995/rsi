# 跨链技能迁移 pilot：源链学习 → 冻结库 → 新链测试

新运行目录：`/data2/ytzhang/rsi-harness-runs/cross-chain-20260929-qwen38-27b-v1`。
这是独立协议，不改变历史链内实验，不将其包装成在线跨链学习。

## 阶段与边界

1. 四条源链：`break-filter-js-from-html`、`chess-best-move`、`bn-fit-modify`、`build-cython-ext`，共 19 题。
   普通 skill-RSI 复用此前已完成的 19 次解题及链末技能库；meta-skill-RSI 接管正在运行的任务，继续本链后续题。
   原 21 链队列留有 PAUSE，不再打开其余 17 链；当前任务不终止、不重启。
2. 两组分别读取**自己的**四条源链库、公开轨迹窗口、题目说明和最终标量分数。每组最多 3 次模型调用，2048/4096/2048 输出 token，600 秒总学习墙钟。
   原始公开窗口每题最多 24000 字符；合并输入再次按预定顺序均匀压缩，带不连续片段标记，总轨迹不超过 24000 字符。
   普通组做技能清点、合并、基本审核；meta 组做带原文索引的观察/假设区分、适用范围及反例提案、证据复核。
   原文片段只能证明观察到的事件，不能自动证明因果解释。复核是 LLM 审查，不是假称的可执行回归测试。
3. 分别冻结长期库。每套库最多 16 个技能、32768 JSON 字节，任务技能与 meta-skill 共用配额。两组不互借技能、轨迹或审核结果。
4. 两套库全部冻结后，才允许任何正式目标解题，包括 baseline。目标阶段**不更新 skill、不修改 harness、不更新 updater**。
   所有目标链读取同一版本；上下文、容器、任务状态仍分别隔离。目标结果不会回流合并器。

## 新目标链与环境

候选四链：`multi-source-data-merger`、`torch-pipeline-parallelism`、`winning-avg-corewars`、`large-scale-text-editing`，各 5 题，共 20 题，三组总计 60 次正式 solve。

选择依据是本地没有找到既有模型解题记录、类别覆盖、缓存环境可复用，不按分数筛选。
`target_exposure_audit.json` 记录检查范围；不声称排除了预训练或其他机器上的接触。
注册时固定候选题目和缓存镜像，可以一边完成源链、一边做独立环境检查；只有一条目标链的全部环境合格才能打开该目标链。环境不合格不能算模型零分，也不能按目标分数另选替代链。

- 原镜像、overlay2、tmux 及评分 runtime 复用，不全量重建。
- 数据合并原型的宿主 parquet 是 LFS 指针，但 prebuilt 镜像包含真实文件；程序核对镜像 ID、文件 SHA-256 与 LFS OID、大小，且只放行这个精确预构建文件。其他 LFS 占位符仍拒绝准入。
- 新目标三组评分上限统一 1800 秒：PyTorch 评分依赖安装较重，文本编辑评分含最多 600 秒的子检查，原整阶段 600 秒不足。评分测试、规则和求解预算不改。历史源链保持原 600 秒评分上限，不能混称完全相同的端到端协议。
- 有效零分保留；环境/服务/评分不完整进入 blocked。中断不能自动重解题；已有 result 仅允许完成记录/检查。

## 模型、资源与调度

Qwen/Qwen3.8-27B，四套现有双卡服务 8040/8041/8042/8044，对应 GPU 0–7；不动其他用户的卡。
求解仍为最多 15 次模型调用/15 turn、120 条命令、每次最多 32768 输出 token、7200 秒 agent 墙钟。检索仍为固定词面 top-4、12000 字符。

三组目标任务采用相同链/题顺序，优先补齐较早同题的三组结果。第四个服务可做另一条链，不要求三组互相等待；同组同链始终顺序执行。只有完成四条源链后才能合并；目标阶段不得提前开始。

## 可比性限制

普通源学习原协议最多 1 次更新调用，meta 源学习最多 3 次；合并预算相同，但不能宣称等总计算量。
四条源链原本各从空库开始，跨链库是在源阶段结束后批量合并得到，不是每条源链之间在线传承。
这里评估的是源到目标的冻结迁移能力，不是跨 benchmark 泛化。合成验收的满分也不是正式 benchmark 成绩。

## 检查与运行

```bash
source scripts/docker-env.sh
.venv-terminal/bin/python -m cross_chain_rsi.campaign \
  --root /data2/ytzhang/rsi-harness-runs/cross-chain-20260929-qwen38-27b-v1 --report
```

`manifest.json` / `seal.json` 固定协议、代码、题目与环境；`banks/*` 保留源输入审计、全部学习调用、复核和不可变库；`status.json` 显示实时阶段及工作进程；`metrics.json` 优先给出三组同题和共同完整链指标。
每 20 秒巡检，每 30 分钟写入 `monitor/` 快照。**这是后台程序巡检留档，不等于自动唤醒 Codex。**

代码在独立 `cross_chain_rsi/` 包内，避免修改正在运行的旧实验冻结源码。

## 部署验收记录

- 项目测试：160 passed、2 skipped、20 subtests passed；其中新增跨链测试 17 项。
- 独立真实模型验收：`/data2/ytzhang/rsi-harness-runs/cross-chain-acceptance-20260929/attempt04`，三组合成目标评分均为 1，两个技能组均实际读取各自 revision 1，库校验和前后不变。
- 前面三次开发验收的原始日志全部保留：attempt01 空源库产生空提案；attempt02 普通组 Markdown 缺标题，改为结构化字段、宿主渲染；attempt03 三组均 1 分，但运行中调度准入代码有修改，因此完整代码校验拒绝通过，未用于正式 seal。attempt04 是最终不变代码的通过记录。
- 2026-09-29 23:24（Asia/Shanghai）完成调度器交接：原 8040 / 8041 工作进程原样接管，8042 续跑 `bn-fit-modify-EVO-1`，8044 续跑 `build-cython-ext-EVO-2`。原已完成的 6 个新方法结果未改动。
- 新 supervisor 初始 PID 2091561；以 `supervisor_launch.json` / `status.json` 的 PID 身份和时间为准。接管后仍处于源链收尾阶段，不是声称 60 次跨链目标 solve 已开始。
- 注册时 17/20 个目标环境已可复用，其余 3 个使用新统一评分时限复查，结果写 `_infra/readiness/`；新任务准入读实时 ticket，不把“已注册候选”当作“环境已通过”。
- `deployment_health.json` 保留部署时检查；后续实时状态以 `status.json`、`metrics.json` 和 readiness ticket 为准。

## 2026-09-30 结果审核与运行恢复

03:12 源链 `break-filter-js-from-html-EVO-3` 被旧规则隔离，调度器随后退出。宿主记录显示：apt 安装 curl 失败，但缓存 uv 恢复成功、pytest 正常运行；唯一失败是公开要求的 `/app/out.html` 不存在。原始 0 分及解题轨迹不改，不重新解题。

本次基础设施修订在 `operational_amendments/1790724699906101898/` 留存旧 seal、代码归档、17 个既有有效结果的哈希、回归报告及新的 seal。题目、预算、冻结模型、solver、源链 updater、检索与跨链合并算法均未修改。

- 新准入规则只处理“curl 引导失败 → 明确恢复本地缓存 uv → 完整一致的测试和 reward”这一窄范围情形。若有失败，必须由断言直接证明题目明确要求的输出文件不存在。任意 AssertionError、其他缺包、测试内网络错误、磁盘错误、测试不完整仍拒绝放行。
- 隔离记录原字节移入 `recoveries/`，已经有完整 trial 的题目只恢复后处理/学习；同一个原始 trial 最多自动恢复一次。隐藏测试/评分日志只用于宿主有效性检查，不传给模型学习。
- 源阶段通过显式适配器调用原冻结 worker；旧 `rsi_harness` 文件和历史独立检查规则均不变。新目标三组采用同一个新准入规则。
- `needs_review` 不再使调度器退出；它继续心跳、保存 `alerts/` 并巡检可恢复工作。真环境异常仍隔离，不变成算法零分。
- 独立 `cross_chain_rsi.watchdog` 每 20 秒检查调度器，至多每小时启动 3 次、至少间隔 60 秒。发现已有活跃 worker 会接管；发现 wrapper 已退、Harbor 子进程仍活跃则保留端口，不叠加新任务。不会因重启调度器而重做题目。
- 回归测试：185 passed、2 skipped、20 subtests passed。复核原 attempt04 的三组真实模型 smoke 和冻结库，全部仍有效；因只改运行/审核层，本次未额外调用模型重跑 smoke。

观察文件：`watchdog/status.json`、`status.json`、`metrics.json`、`alerts/`、`result_admissions/`、`recoveries/`。两层巡检仍是后台程序，**不是自动唤醒 Codex**。

恢复实测结果：EVO-3 原 0 分被保留，未重跑 solver；原来的三阶段 updater 首次执行，3 次调用、约 96.8 秒，提交 revision 3。EVO-4 已实际读入 revision 3 并开始解题，源阶段推进到 18/19。

07:41 完成一次受控的“仅调度器进程退出”演练：watchdog 自动启动替代调度器，仍在解题的 worker PID 3986517、身份、launch 文件和此前 18 个完成记录均不变，没有第二次 solver 启动。证据见 `watchdog/restart_drill/`。守护 PID 初始为 3979459，演练后调度 PID 为 3994667；之后仍以实时身份文件为准。
