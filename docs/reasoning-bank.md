# ReasoningBank：七条链逐题更新

这是新增的一组对照，原有五组配置不变。官方源码固定在子模块
`upstream/reasoning-bank`，commit 为
`ed80611788292ea739f1effd31f16c53823b8a0d`。

## 协议与适配

- 同一 KIRA 初始求解器、DeepSeek V4.1 Flash / deepinfra fp8、high、temperature=1、top_p=0.95。
- 上下文 262144、单次输出上限 65536、100 轮、求解 7200 秒、评分 600 秒；无单题累计 Token 上限。
- 七条链各 15 题；链内按官方顺序，链间并行。每条链从空记忆开始。
- 每题结束后，包括末题，用原版 self-judge 和成功/失败 extraction prompt 更新记忆。
- judge temperature=0、extractor temperature=1；均使用相同 DeepSeek 模型及 high 推理。
- 原版 append-only bank、检索一个历史 experience、最多三个 memory items 的原始提示；不使用 MaTTS。
- 原版 Gemini embedding-001 / 3072 维，原始 instruction-aware query、归一化、相似度与选择函数。
- 不向 self-judge/extractor 额外传入本题的真实评分、私有测试或参考补丁。公开求解轨迹中的既往 milestone success 标记与其他 KIRA 组一致。

官方项目默认的 mini-SWE-Agent 被统一 KIRA 替代。Harbor ATIF 公开动作/观察映射为轨迹；原版记忆前缀注入 KIRA system message。Python 3.12 公共环境与官方项目根依赖不同，因此从固定源文件加载原始函数 AST，仅替换 Google SDK 的模型调用边界，使用统一账本记录费用。不是论文原始配置的逐字复现。

Google embedding 通过 OpenRouter 请求同一模型、维度；原版 `RETRIEVAL_DOCUMENT` 映射到 OpenRouter 支持的 `input_type` 字段，不能直接传会被忽略的 `task_type`。实测 `search_document` 与 `RETRIEVAL_DOCUMENT` 返回相同向量，`search_query` 则不同。原版检索、judge、extraction 和 memory assembly 函数保留。Gemini 没有公开模型权重。

## 环境

先完成现有 `docs/reproduction.md` 的公共环境检查。额外 PyTorch 采用独立目录，避免改变原有 138 个依赖：

```bash
python -m pip install --target .work/reasoningbank-deps --no-deps \
  --index-url https://download.pytorch.org/whl/cpu torch==2.9.0+cpu
python -m pip install --target .work/reasoningbank-deps --no-deps \
  sympy==1.14.0 mpmath==1.3.0 networkx==3.6
export RSI_RB_DEPS="$PWD/.work/reasoningbank-deps"
```

离线任务还需要原实验的运行包；在启动 watcher 前配置以下变量。它们由子进程继承，不能只验证 Docker 和 Python 就省略。首题缺少 tmux 时可能在任何模型调用前失败，Noop probe 无法覆盖这个 agent setup 步骤。

```bash
export HARBOR_FORMAL_REQUIRE_CLEAN_VERIFIER=1
export HARBOR_TMUX_BUNDLE=/path/to/tmux-runtime-singleuid-amd64.tar.gz
export HARBOR_VERIFIER_BUNDLE=/path/to/verifier-runtime-singleuid-amd64.tar.gz
```

复用经过校验的原运行包，不从任意任务镜像在线安装不同版本。

通过 `scripts/reasoning_bank.py prepare --help` 查看本机路径参数。准备阶段重用已校准的不可变任务镜像与私有评分映射，检查公共模型配置及源码指纹；不复制历史答案或已学记忆。`--embedding-proxy` 只控制 embedding 的路由，求解 API 保持既有直连配置。

启动命令：

```bash
python scripts/reasoning_bank_watch.py --campaign /path/to/campaign
python scripts/reasoning_bank.py status --campaign /path/to/campaign
python scripts/reasoning_bank.py pause --campaign /path/to/campaign
```

watcher 先运行 Fiber 两题，确认求解、两次更新与第二题实际记忆注入，然后启动七条链。启动需要本机私有密钥文件，且用户已经授权付费实验。暂停在当前任务/更新结束后生效。

## 费用和恢复

所有链、task、judge、extraction、embedding 与失败重试共用一个 50 美元 SQLite 账本。embedding 输入 Token 计入 RSI Tokens，保留独立阶段明细。缓存命中仍计入 provider 返回的 input tokens。未知账单保留上界，不按零处理；已保存的付费成功响应直接恢复，不再次生成。

识别出的网络故障等待后自动重试；配置或算法异常保存 needs_review，禁止盲目计为答错或抹掉费用。运行使用冻结代码目录；修改工作仓库不会改变运行中的实验。

记忆阶段保留完整公开轨迹，并将单次输出上限收小至固定 262144 上下文剩余空间，最大仍为原版 65536；不扩大上下文、不截断轨迹。仅有 `stop` 结束、无正文、无拒绝信息、无工具调用的异常空响应允许相同参数重试一次，原响应和两次费用都保留，成功响应不会重采样。剩余窗口连一个输出 token 都容不下时继续保存 needs_review。

若一次重试后仍无可用记忆正文，按官方 `process_futures()` 对单题更新异常的处理方式记录失败，保留旧 bank 并继续下一题；不编造记忆、不重做该题。该题 query 的 embedding 缓存仍保留，和原版 `screening()` 在任务开始时写缓存一致。原版 top-k 命中无对应 bank 条目时返回空记忆，不改选次优条目。指标分别报告更新尝试、成功更新、失败更新与实际 bank 条目数；预算、网络、输入超长及未识别异常仍单独处理。

费用未知的请求可以在所有模型调用停止后，以认证 API key 累计账单核对总预算。单次费用纳美元向上舍入可能使本地已知费用略高于供应商累计值，预算按二者较大值保守记录。核对只消除货币预留的不确定性，保留原始未知 Token 标记。

原有 Click 对照和 CoreDNS 旧 EvoSkill 结果仍有各自历史审计要求；新增七链结果不会自动消除这些问题。相同求解限制与完整费用统计也不等于已经完成相同总 Token 预算对照。
