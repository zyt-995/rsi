# 实现说明

## 1. 可信边界

```text
                         trusted host
 BenchmarkAdapter ← Runner / CostTracker → append-only StateStore
        │                  │                         │
 evaluator/hidden      EventLog                artifact revisions
 tests/reward              │
                       AgentBackend
                           │
                 active executable program
                   (isolated container)
```

Runner 独占调度、动作授权和计费；benchmark adapter 独占环境和评分；模型只能提出动作。
模型生成的程序从 stdin 接收公开 request，只能返回一个 task/self action 或委托 backend 的附加指令。
它看不到 evaluator metadata、未来任务、隐藏答案、凭据或可写宿主目录。

## 2. 四种 RSI 范式

| 范式 | mode | 修改时机 | 修改对象 | 提交规则 |
| --- | --- | --- | --- | --- |
| Fixed Loop | `fixed-loop` | 每题结束后的固定阶段 | 有界 artifacts | candidate 与 active 行为回放不退化才生成 revision |
| Self-Improve-as-Action | `self-action` | rollout 内由策略选择，并有终局 `reflect` | 有界 artifacts | 同一验证门后立即可见 |
| Open-Ended Self-Mod | `open-ended` | task 后改进阶段 | 完整多文件 executable agent | 严格优于 active 后沿单谱系部署 |
| Search/Evolution | `evolution` | task 后 K 个 search slots | 同一 executable surface | 同父候选全部验证归档，按 reward/模型调用稳定择优 |

`static` 与 `context-only` 是两个对照。所有方法冻结模型权重；不同 mode 共用任务序列、动作计费、
日志格式和 evaluator。搜索不是免费：提案、validation backend 调用、失败候选和部署动作均扣预算。

## 3. 可执行修改

`ProgramStore` 保存 `versions/000000...`、candidate archive 和唯一 `HEAD.json`。candidate 至少包含
`agent.py`，文件类型和总字节数有白名单。部署与回滚只追加 revision。

默认 `DockerProgramExecutor` 使用 `python:3.10-slim` 长驻 worker：

- `--network none`
- read-only root filesystem
- `--cap-drop ALL`
- `no-new-privileges`
- 1 CPU、256 MiB、64 PIDs
- program store 只读挂载
- 临时目录为 noexec tmpfs

candidate 先通过 JSON 协议 smoke，再在当前任务和最多 N 个兼容历史任务的私有 replay case 上比较
candidate 与 active program；两者看到相同的公开 request。没有行为 case 时禁止部署。task score 必须
严格提高；仅当双方都取得正分时，较少 delegated model calls 可作为同分改进。validation 不推进正式环境。
候选可替换 `agent.py`、`workflow.py`、prompt/data 文件组成的完整程序，改进阶段可看到 active 的完整源码。

## 4. 状态和成本

Artifact API 只允许 `MEMORY.md`、`AGENTS.md`、`skills/...`、`scripts/...`，限制总字节数并保存完整
diff。Context-only 使用独立 FIFO 字节上限。任务内 trajectory 每题清空。

宿主在执行前扣固定 decision/action units；SI 有独立子上限。另记录 backend 报告的 input/output
tokens、backend 调用、program runs 及两类 wall time。直接程序决策没有 provider tokens，因此
`token_usage_complete` 只检查真实 backend calls。未上报 usage 时为 false。预算耗尽后的任务仍保留在
评估分母中，reward 为 0。

`matrix` 批量展开 seed × mode，每个 cell 都是不可覆盖的独立 run；`analyze` 递归发现完整 run，生成
逐 run CSV、逐 task CSV、按 mode 汇总 JSON 和无需额外依赖的 SVG learning curve。

## 5. Benchmark 接入

### EvoArena / PersonaMem-Evo

`PersonaMemEvo` 直接读取发布的 10-person CSV 和 32k chat histories，固定 seed 打乱选项并做 exact
MCQ scoring。检索是项目内 BM25-like 实现，因此 manifest 明确标记为 direct adapter，而不是上游
A-Mem runner 复现。过去已完成题可作为 program validation replay。

### ContinualSkillBench

使用锁定 commit 的 Harbor CLI。项目本机没有系统级 Docker 权限，因此使用专用 rootless daemon。
兼容补丁把 `docker cp` 换成容器内解压 root-owned tar，避开单 UID namespace 的 `lchown` 限制；补丁
保存在 `benchmarks/patches/continual-skill-bench-rootless.patch` 并由 setup 脚本幂等应用。

### SWE-Chain

保留上游 `run.sh` / `eval.sh`、链数据和 oracle。安装 smoke 构建真实 `attrs 21.3.0` 镜像，并在
`network=none` 下运行其测试。正式 chain 仍需按所选链预构建每个版本镜像。

## 6. 网络阶段

安装阶段允许从 PyPI、镜像 registry 和数据仓库下载。正式实验目标是：任务容器无公网、只允许访问
本机模型端点；生成程序始终完全断网。当前 `preflight.formal_offline_ready=false` 是有意的，因为
全部 CSB task/verifier 依赖和全部 SWE-Chain 版本镜像尚未预烘焙。任何依赖实时网页的任务必须排除，
或替换为固定 snapshot/mock。

## 7. 已验证范围

- 28 个 unit/integration tests，包括预算、隔离、本地模型 bridge、终局反思、多文件源码、无验证拒绝、同父种群、
  candidate 验证/激活/淘汰/实际使用、汇总分析和回滚。
- 四 RSI mode 的真实 Docker program-executor matrix smoke：3 次 artifact 更新、3 次 open-ended 部署、
  9 个 evolution candidates / 3 次选择；并生成 JSON/CSV/SVG 分析产物。
- PersonaMem-Evo 发布数据 + 真实模型一题 smoke。
- CSB Harbor/oracle/offline-verifier 完整 smoke。
- SWE-Chain 官方 Dockerfile、真实源码、断网测试 smoke。

这些结果证明接口和代表性运行路径可用，不等同于三套 benchmark 的全量正式实验已执行。
