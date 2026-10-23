# 上游与修改记录

完整 URL、commit、数据 revision 见 `upstream/manifest.json`。`git clone --recurse-submodules` 获取方法源代码；不要直接升级子模块。若缺少提交或网络失败，应记录阻塞，不可换最新 main 后声称同一实验。

- Meta-Harness：官方 KIRA 初始 agent、搜索与 frontier 逻辑；SWE 在线适配位于本仓库。
- EvoSkill：官方 loop、proposer、generator、ProgramManager；适配位于 evoskill_swe。
- ModularRSI：保留官方版本和此前适配，未列入当前五组便携启动入口。
- EvoArena：保留基准来源；实际 SWE-Chain-Evo 数据通过固定 Hugging Face revision 获取。
- Harbor：必须安装 **upstream/harbor + patches/harbor-runtime.patch**。该补丁来自已运行实验的 tracked diff，涉及 Docker、verifier、tmux、LLM/agent 接口。仅安装 PyPI Harbor 不能宣称是相同运行环境。

Harbor 应用补丁后子模块显示 dirty 是预期现象；其他官方方法源文件保持固定 commit。正式准备后 source pins 会记录文件 SHA，包括上游与本机 Claude binary。

`source-provenance.json` 保存迁移前来源文件 hash；迁移修改主要为可配置路径、可安装布局和新环境入口，另合入已有 Istio grader fixture 修复。实验方法参数和算法不因迁移而改变。上游各自的许可证见其 LICENSE；本仓库不替第三方代码重新授权。
