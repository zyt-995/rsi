# 迁移验证（2026-10-23）

- 在新建 Python 3.12 venv 安装依赖和 editable 包；pip check 通过，setup.sh 实际执行通过。
- 43 项离线测试通过，0 失败、0 跳过；包括官方 EvoSkill loop 的 fake-provider 更新、长轨迹缩减、记忆隔离和账本、真实 bwrap 隐藏私有缓存、新 workspace 的五组配置与 source pins。
- 4 个上游 Pydantic/Harbor API 弃用告警，不影响本次测试结果。
- 固定清单的 105 个实例 SHA 与本机已有固定数据逐一一致；本次未重新从远端下载整个数据集。
- 实际 Fiber 第 1 题：参考解 reward=1、空提交 reward=0、实际 Harbor Noop 执行和评分通过。使用已有固定镜像，未重新下载大镜像。
- 初次 Harbor 检查因本机自定义 Compose 插件目录未设置失败；补充 DOCKER_CONFIG 后通过。失败诊断保留在本机忽略目录。
- doctor 的依赖、版本、user namespace、Docker daemon 与 Compose 检查通过；skill 格式校验通过。
- 待提交文件检查未检出所检查的 API/private-key 格式、本机绝对路径或超过 5 MiB 的文件。

本次没有调用付费模型，也没有运行完整 15 题链。因此这些检查证明离线接入和已验证单题可用，不代替协作者机器上的完整环境准备与首次付费 task/update 检查。结构化结果见 validation.json。

## 完整依赖锁与源机器对齐补充

原先仅固定直接依赖的安装会解析出更新的间接依赖。已重新创建干净 venv，按原实验 core + SDK fallback 的有效版本锁定 138 个运行依赖，pip check 通过。新增一致性检查与配置漂移测试后，全套 46 项测试通过；完整环境对齐检查 passed=true。固定依赖环境下再次实际执行 Fiber Harbor Noop，通过且模型调用数为 0。

启动入口会核对 134 个文件的指纹、上游版本/补丁、Python patch 版本、运行依赖、系统工具和实际 session 参数。参照为最新修复后的协议；没有复制旧批次的已知错误或未完成学习状态。
