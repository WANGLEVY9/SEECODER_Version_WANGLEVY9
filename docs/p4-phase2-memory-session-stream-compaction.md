# P4 Phase-2：记忆注入 + 会话持久化/恢复 + 流式 + 上下文压缩（已实现并验证）

> 完成日期：2026-08-27。全部自研，未引入 Agent 框架/SDK；仅用 `openai` 客户端 + 原生 tool calling。

## 1. 新增/修改模块

| 模块 | 内容 |
| --- | --- |
| `memory.py`（新） | 读取工作区 `SEECODER.md`/`AGENTS.md`，注入系统上下文（运行时始终可见） |
| `session.py` | 新增 `save`/`load`/`restore` 序列化；CLI `chat --resume/--save`；注入记忆 |
| `model_client.py` | 新增 `complete_stream`：累积 content/tool_calls/reasoning/usage，产出增量 `StreamEvent`；`RetryingModelClient.complete_stream` 转发 |
| `types.py` | 新增 `StreamEvent` |
| `runner.py` | `stream_sink` 流式路径 + `compactor` 压缩器（默认关，thinking 模式禁用）+ `build_system` 记忆注入 |
| `compaction.py`（新） | `compactable_prefix`/`summarize`：把超预算的老回合压成 `<compacted_context>` 笔记 |
| `config.py` | `compaction_enabled`（`SEECODER_COMPACTION`，默认关） |
| `cli.py` | `chat --resume/--save`；`SEECODER_COMPACTION` 时启用模型压缩器；json 模式下流式下发 `token` 事件 |
| `desktop/electron/renderer` | 渲染流式 `token` 增量文本 |

## 2. 关键设计

- **记忆**：约定式，非专用工具。`SEECODER.md`/`AGENTS.md` 在会话开始钉进系统提示，模型始终可见；agent 仍可用普通文件工具读写它。
- **流式**：runner 仅在设置了 `stream_sink` 时走流式；否则回退非流式，保证测试/CLI 兼容。`complete_stream` 自行组装 tool_calls 与 usage，产出 `done` 事件；流式重试不做（已消费的流不可重放）。
- **压缩**：默认关闭（`SEECODER_COMPACTION=0`），避免改变既有确定性裁剪。开启后仅在历史超预算且非 thinking 模式时触发：保留 system+初始任务+最近 `N` 个完整回合，用一次摘要调用压缩其余部分。压缩失败回退确定性裁剪。
- **会话**：`Conversation.save/load` 序列化消息（含 tool_calls/reasoning）、模式、usage、步数；恢复时重建 runner 并重注入记忆。

## 3. 离线测试（干净环境，60 项全绿）

新增记忆注入、流式 delta + 流式工具调用、压缩折叠 + thinking 跳过、会话 save/load 往返等用例。Electron 核心测试 3 项通过。

## 4. 真实 API 受控验证（deepseek-v4-flash）

1. **流式**：`--event-json` 下发出 `token`（"P"→"ONG"）与 `usage`(1177 tokens) 事件。
2. **记忆**：临时工作区放 `SEECODER.md`（"修复 normalize_tag 空白处理"），agent 不读文件即转述出该目标——系统提示注入生效。
3. **会话恢复**：`chat --save` 后 `--resume` 续聊，第二轮正确回答（"1+1=2"→"2+2=4"），恢复修复了 `Conversation.load` 不接受 `mode` 参数的 bug（现改由存档恢复模式）。

## 5. 仍待做（见 roadmap P3）

代码索引语义检索、并行工具调用、web_search、子代理、更强执行隔离、会话自动检查点。
