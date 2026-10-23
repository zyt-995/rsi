# 环境准备与复现

## 主机要求

Linux x86_64，Python 3.12，Git、Docker + Compose v2、bubblewrap、uv、Go 1.25.0、原生 Claude Code 2.1.299。GPU 不需要。Go 用于私有评分依赖补全；宿主可联网下载公开模块，求解器和评分容器仍保持禁网。Claude 需要本机原生可执行文件，当前 bwrap 挂载实现不支持 npm shell wrapper 替代。

先设置 `.env`，再执行安装：

```bash
cp .env.example .env
# 编辑 RSI_WORKSPACE 等；无需填写 API key 即可准备环境。
source .env
bash scripts/setup.sh
source .venv/bin/activate
python scripts/doctor.py --docker
python -m pytest tests
```

系统工具自行用本机包管理器或官方发行包安装；`setup.sh` 不改 Docker daemon，不安装系统软件或下载任务镜像。它初始化固定子模块、应用 Harbor 补丁、建 Python venv 并安装依赖。`requirements.lock` 固定直接依赖版本；完整源环境版本另存于 `upstream/python-environment.json`，并不宣称所有传递依赖均已锁定。

`RSI_DOCKER_BINARY`、`DOCKER_HOST`、`DOCKER_CONFIG`（自定义 Compose 插件目录）、`RSI_GO_BINARY` 可配置已有工具。将 uv、claude 放进 PATH。安装和运行使用同一个 Python venv；bootstrap 会拒绝把同一 workspace 指向另一个解释器，避免混用运行环境。

## 存储与下载

运行目录默认 `.work`，可设 `export RSI_WORKSPACE=/large-disk/rsi-work`；Docker 的 data-root 必须也有足够空间，仅改变 RSI_WORKSPACE 不会搬迁 Docker 镜像。工作目录、镜像 tar、dataset、tokenizer、日志、私有评分缓存不进 Git。

```bash
python scripts/fetch_data.py
python scripts/prepare.py --chain fiber --plan
python scripts/prepare.py --chain fiber --indices 0 --environment-only
# 上一步只准备并校准第一题；完整链需下面命令。
python scripts/prepare.py --chain fiber --model deepseek --budget-usd 50
```

`--plan` 输出官方镜像压缩包大小；Docker 展开层、隔离 solver 镜像、并发工作区还需要额外空间，不能按压缩大小配盘。准备单条链后再准备下一条，避免默认下载七链全部镜像。`HF_ENDPOINT` 可切换可信的 Hugging Face 镜像；校验固定数据 revision、实例 SHA 与镜像 receipt。

第一题校准执行参考补丁应得 1、空补丁应得 0。完整链准备另运行实际 Harbor Noop 流程。所有这些检查均不需要 API key，不调用模型。DeepSeek tokenizer 按固定 SHA 检查；若上游 main 变动，不应悄悄接受不同文件，需取得匹配版本或建立新实验版本。

## 启动与恢复

设置本地 OPENROUTER_API_KEY 后，按 README 的 `run.py start` 命令启动。它写入本地启动记录，四个 coordinator 并行管理五组。准备和测试不会自动进入此步骤。

- 多条链同时运行需不同 `--port`（每 session 使用起始端口及后三个端口）和不同 session 路径。
- `run.py pause` 在任务/更新边界暂停；已运行的任务可能继续到边界，不能将它理解为即时取消 API 请求。
- 余额不足、未知费用或中断任务需要检查保存的账本和轨迹再恢复，不能删数据库“重新开始”。
- 换机器重新准备环境并生成本机路径和 source pins；不要照搬旧机器 configuration 的绝对路径。
- 对现有运行修改代码后，source pins 会阻止继续混用。建立新版本并记录修复，而不是改旧成绩。

离线单元测试使用合成轨迹与 fake provider。真实 bwrap 隔离检查要求系统允许 user namespace；不支持时该项跳过，doctor 和付费 launch 前仍需部署方确认真实隔离可用。

零模型 probe 失败后先修复报错，再将该次 `probe/` 目录移到旁边的诊断归档后重试；脚本不自动覆盖失败记录。不要对付费 trial 使用此操作。
