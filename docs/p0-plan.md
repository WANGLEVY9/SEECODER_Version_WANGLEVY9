# SEECODER P0：最小 Coding Agent 闭环

## 1. 目标和完成定义

P0 的唯一目标是交付一个可从命令行运行的、单轮任务驱动的 coding agent。它接受一条自然语言编程任务，使用模型原生的 tool calling 在指定工作目录内读取和写入文件、执行命令，并在成功、可恢复失败或达到停止条件时结束。

P0 完成时必须满足：

- 不依赖任何 agent 框架或 agent SDK；核心循环、上下文、工具调度和错误处理均在本仓库实现。
- 至少提供 `list_files`、`read_file`、`write_file` 与 `run_command` 四个本地工具。
- 模型只通过原生结构化 tool calls 请求工具；伪造的文本命令不执行。
- 每次工具调用都有参数校验、受限工作目录、结构化结果和日志记录。
- 系统能在无工具调用时结束，也能在最大步数、连续工具错误、模型错误和 Ctrl+C 时可解释地结束。
- 核心循环和本地工具可在不调用真实模型 API 的情况下由测试验证。

P0 不包含：GUI/TUI、多 agent、RAG、长期记忆、自动摘要、多模型路由、联网搜索、插件体系或端到端基准评测。

## 2. 选定技术方案

- 语言与版本：Python 3.12。
- 包管理：`uv` 与 `pyproject.toml`。
- 模型接入：官方 `openai` 客户端，仅使用 Chat Completions 的原生 tool calling；模型名、Base URL 和 API Key 均由环境变量提供。
- 交互形态：CLI。建议首个命令为 `uv run seecoder run "<任务>" --workspace <路径>`。
- 运行模型：P0 只保证一个 OpenAI 兼容提供方。后续如需支持其他厂商，以小型 adapter 扩展，不修改 agent loop。

该方案直接使用模型厂商客户端库，不引入 LangChain、OpenAI Agents SDK 或任何托管执行/文件 API，符合题目边界。

## 3. 系统边界

```text
CLI task + workspace
        |
        v
AgentRunner ----> ContextManager ----> ModelClient
    |                                       |
    |                                   native tool calls
    v                                       |
ToolRegistry <---- ToolDispatcher <----------+
    |
    v
Local filesystem / subprocess (workspace-bound)
```

模型负责提出下一步操作；程序负责验证、执行、记录和决定何时停止。模型不能直接访问文件系统、终端或环境变量。

## 4. 建议的项目结构

```text
src/seecoder/
  __init__.py
  cli.py                 # 参数、退出码、控制台输出
  config.py              # 环境变量和运行参数
  types.py               # 消息、事件、工具结果、终止原因
  runner.py              # Agent 状态机和主循环
  context.py             # 上下文预算与裁剪
  model_client.py        # OpenAI-compatible API adapter
  tools/
    __init__.py          # 注册表、schema 和统一 dispatch
    files.py             # list/read/write，根目录检查
    shell.py             # run_command，超时和输出截断
  trace.py               # JSONL execution trace
tests/
  test_tools.py
  test_context.py
  test_runner.py
  fakes.py
demo_workspace/          # 一个可重复的 bug-fix 演示项目
pyproject.toml
.env.example
.gitignore
README.md                # 开发与使用文档；提交时另备 README.txt
```

## 5. P0 工具契约

所有工具返回同一结构：

```json
{"ok": true, "data": {}, "meta": {"truncated": false}}
```

失败不抛给主循环，而返回：

```json
{"ok": false, "error": {"kind": "ValidationError", "message": "..."}}
```

| 工具 | 输入 | 行为与限制 |
| --- | --- | --- |
| `list_files` | `path="."`, `max_entries=200` | 列出工作目录内条目；忽略 `.git`、`.venv`、`__pycache__` 等默认噪声目录。 |
| `read_file` | `path`, 可选 `start_line`, `end_line` | UTF-8 文本读取；单次读取限制行数和字符数，返回行号；二进制或目录返回工具错误。 |
| `write_file` | `path`, `content` | 仅写入工作目录内路径；自动创建父目录；结果含写入字节数。P0 不支持工作目录外写入。 |
| `run_command` | `command`, 可选 `timeout_s` | 在工作目录执行，默认 30 秒、最大 120 秒；捕获 stdout、stderr、退出码和超时；输出截断。 |

路径检查必须在解析绝对路径和符号链接后进行，防止 `../` 或符号链接逃逸。命令由模型生成，因此日志中记录命令、目录、耗时和退出码；P0 对明显高风险命令建立最小拒绝列表（例如递归删除工作目录、`git reset --hard`），并持续排空但只保留有界输出。命令以工作目录为起点执行，但不构成操作系统级沙箱。

## 6. Agent 状态机

```text
INIT
  -> MODEL_REQUEST
  -> TOOL_DISPATCH -> MODEL_REQUEST        (收到一个或多个有效 tool call)
  -> FINAL                                (模型给出最终文本且无 tool call)
  -> STOP_MAX_STEPS                       (达到 max_steps)
  -> STOP_TOOL_ERROR_LIMIT                (连续工具错误达到阈值)
  -> FAILED_MODEL                         (重试后仍无法调用模型)
  -> CANCELLED                            (Ctrl+C)
```

默认值：`max_steps=16`、`max_consecutive_tool_errors=4`、模型瞬时错误最多重试 3 次。每轮先调用模型；若包含多个 tool calls，按模型给定顺序串行执行，逐个将结果回填给模型。不得因工具失败而丢失上下文或直接崩溃。

## 7. 上下文和输出策略

- 永远保留 system prompt、用户原始任务和在预算内尽可能多的最近完整工具回合。
- 文件内容单次最多 12,000 字符，命令输出最多 8,000 字符，并在结果中标记截断。
- 整体上下文默认控制在约 45,000 字符内；超额时优先剔除最早的长工具输出，不删除初始任务和最近行动轨迹。
- 结构化、已脱敏的运行证据写入启动目录下的 `runs/<run_id>.jsonl`，该目录被 `.gitignore` 忽略；工具输出与上下文均有上限。
- P0 只做确定性的裁剪，不增加需要额外模型调用的自动摘要功能。

## 8. 配置和凭据准备

`.env.example` 只提供变量名与非敏感示例：

```dotenv
SEECODER_API_KEY=
SEECODER_BASE_URL=https://api.deepseek.com
SEECODER_MODEL=deepseek-v4-flash
SEECODER_THINKING_MODE=disabled
SEECODER_MAX_STEPS=16
```

真实 `.env`、运行日志、视频源文件和本地演示产物必须写入 `.gitignore`。真实 `.env` 必须位于 Agent 可编辑工作目录之外；程序启动时只报告缺失变量名称，绝不回显密钥或请求头。

## 9. 实施顺序

1. 建立 `pyproject.toml`、`src/` 布局、`.gitignore`、`.env.example` 和基础 CLI；提交一次可运行的 `--help`。
2. 实现 `types.py`、工具注册与四个本地工具，先用单元测试覆盖路径边界、编码错误、超时和输出截断。
3. 实现假的 `ModelClient` 与 `AgentRunner`，测试“读文件 -> 写文件 -> 跑测试 -> 最终回答”的循环，无需真实 API。
4. 接入真实 OpenAI-compatible client，完成原生 tool calling 的请求与回填；加入 API 重试与配置校验。
5. 实现上下文预算、JSONL trace、Ctrl+C 退出码和可读的终端摘要。
6. 制作 `demo_workspace`：一个带失败测试的极小 Python 项目，固定任务为定位、修复并验证一个真实缺陷。

每一步完成即提交一次，提交信息应描述可观察能力，例如 `feat: add workspace-bound file tools`，不要在截止前压缩历史。

## 10. P0 验收清单

- [ ] `uv run seecoder --help` 可运行。
- [ ] 缺少 API Key、模型名或工作目录时，CLI 明确失败且不泄露敏感信息。
- [ ] fake model 驱动的测试能覆盖至少两轮工具调用与最终回答。
- [ ] 读取或写入工作目录外路径被拒绝，包括通过符号链接逃逸。
- [ ] 超时命令返回结构化 timeout 结果，进程被终止。
- [ ] 无效 JSON 参数、未知工具、读取二进制文件和命令非零退出均不会让 runner 崩溃。
- [ ] 达到最大步数或连续错误阈值时，输出明确终止原因并保留 JSONL trace。
- [ ] 真实模型可在 demo workspace 中完成一次“读代码、修改、运行测试”的演示。
- [ ] 仓库检查确认 `.env`、`runs/` 与凭据未被跟踪。

## 11. 面试可解释的取舍

- 选择 CLI：考核重点是 agent 内核而不是界面，CLI 使每轮行为与日志可见，降低演示故障率。
- 选择一个模型提供方：先验证核心闭环，避免多供应商的消息格式差异掩盖 agent 逻辑。
- 使用原生 tool calling：允许且更可靠；工具 schema、参数校验和本地执行仍完全自建。
- 固定工作目录与超时：既保留自主执行能力，又将文件和命令风险限制在明确边界内。
- 不做自动摘要：P0 采用确定性裁剪，便于测试和答辩；复杂记忆机制属于后续增量。
