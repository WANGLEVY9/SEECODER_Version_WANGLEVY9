# P1：DeepSeek 联调、受控执行与增量编辑

## 目标

在不扩大为多 Agent 或 GUI 项目的前提下，让 P0 成为可用、可解释、可稳定演示的 DeepSeek V4 Flash coding agent。P1 的优先级是正确性和可控性，其次才是工具丰富度。

## 当前验收状态（2026-08-27）

- P1.1 已完成：thinking 模式保留 `reasoning_content`，预算不足时以命名状态终止而不是删除必要历史；trace 仅保留推理长度与哈希。
- P1.2 已完成：完成无工具连通、thinking 工具协议和临时 workspace 修复闭环；具体可复现实验边界见 `p1-validation-2026-08-27.md`。
- P1.3 已完成：`search_files`、精确上下文 `apply_patch` 与只读 `git_diff` 已接入并经离线测试与真实闭环验证。
- P1.4 已完成：正式 CLI 默认使用 `restricted` argv 模式；`--host-shell` 为显式、带警告的兼容选项。
- P1.5 尚未完成：录屏、三次连续预演与最终交付物打包必须在临近提交时执行，避免将临时 trace 或密钥带入交付物。

## 约束

- 继续只使用模型厂商客户端和原生 tool calling，不引入 Agent 框架或托管执行工具。
- 真实 API 测试必须限定预算、步数和 workspace，trace 不得包含密钥。
- 所有新工具必须有 schema、参数校验、失败结构化返回和离线测试。
- 每个阶段通过后才进入下一阶段，并产生独立 Git 提交。

## P1.1：DeepSeek thinking-aware adapter（必须先做）

DeepSeek V4 Flash 默认启用 thinking；官方要求工具调用回合的 `reasoning_content` 在全部后续请求中原样回传，否则会得到 400。P0 的 `disabled` 模式是基线，P1 将实现可选的 `enabled` 模式。

工作项：

- 扩展 `ChatMessage` 与 `ModelResponse`，包含可选 `reasoning_content`。
- 在 DeepSeek 响应中读取该字段，并在每个 assistant 工具调用消息中原样序列化回传。
- thinking 模式下采用“不可破坏工具回合”的上下文策略：若预算不足，不静默删除必要 reasoning，而以明确终止原因停止并建议重启/增大预算。
- trace 默认只记录 reasoning 的长度、哈希与调用 ID，不持久化原始推理文本。
- 增加 provider-fixture 测试：第一轮工具调用含 reasoning，第二轮请求仍含完全相同字段；缺失字段时给出明确诊断。
- 添加 `SEECODER_REASONING_EFFORT=low|high|max` 配置，并仅在 DeepSeek thinking 模式下传递。

验收：禁用模式和启用模式都可通过离线协议测试；启用模式的受控真实工具任务不出现 reasoning-content 400。

## P1.2：真实 API 分级联调（必须）

1. **连通性**：最大 1 步、无工具，要求模型回传固定短句；记录响应模型名、耗时和 token 用量，不记录密钥。
2. **工具协议**：在临时、无凭据 workspace 中让模型执行一次 `list_files` 与 `read_file`，最多 3 步。
3. **演示闭环**：在 `demo_workspace` 修复 `normalize_tag`，运行 `python -m unittest discover -s tests -v`，检查最终 diff、命令退出码和 trace。
4. **失败路径**：人工设置无效模型名或极低 max steps，确认退出码与日志可解释。

每次测试设置明确的最大步数与成本上限；只有前一层成功才运行下一层。

## P1.3：增量编辑与检索（应做）

- `search_files`：有目录范围、命中数、单行和总输出上限；默认排除 `.git`、虚拟环境、trace 与敏感路径。
- `apply_patch`：使用“精确旧文本 + 期望出现次数”的小范围补丁；先校验补丁上下文，再原子写入，失败时不修改文件。
- `git_diff`：只读展示 workspace 内的 Git 改动摘要，帮助模型与用户确认实际变更。
- 所有工具加入乱码、二进制、超大输入、路径逃逸、无效参数和重复调用测试。

验收：Agent 能先搜索再做小范围 patch，失败 patch 不损坏文件，最终 git diff 与总结一致。

## P1.4：受控命令执行（应做）

引入两种显式模式：

- `restricted`（默认）：禁止 shell 元字符和解释器内联脚本，仅允许测试、格式化、构建和 Git 只读等命令的参数数组执行。
- `host-shell`（显式开关）：保留当前 shell 能力，但在启动时显示“非沙箱”警告。

P1 不把黑名单宣传为安全沙箱。若需要更强隔离，后续版本单独评估容器或 macOS sandbox，而不是仓促在演示前引入不稳定依赖。

验收：restricted 模式可完成 demo 的测试命令，且拒绝重定向、管道、命令替换、绝对路径和 workspace 外路径。

## P1.5：演示与答辩准备（应做）

- 固化一个独立的录制 workspace，原始 fixture 永远保持失败状态。
- 连续预演三次，记录成功率、步数、耗时和 token 成本。
- 输出最终摘要模板：修改文件、验证命令、退出码、停止原因、剩余不确定性。
- 准备答辩说明：为什么单 Agent、为什么工具本地执行、为什么 P0 默认关闭 thinking、如何在 P1 安全启用 reasoning。

## 推荐执行顺序

1. P1.1 protocol tests 和 thinking-aware adapter。
2. P1.2 先无工具、再工具、再完整修复的真实联调。
3. P1.3 `search_files`、`apply_patch`、`git_diff`。
4. P1.4 restricted command mode。
5. P1.5 预演、录制脚本和答辩材料。

在 P1.2 的真实闭环成功且 P1.4 的受限模式可用前，不扩展多模型、多 Agent、RAG、GUI 或网络搜索。
