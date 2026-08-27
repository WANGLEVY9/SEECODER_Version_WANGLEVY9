# SEECODER：从`半个 agent`到`真正的 coding agent`方案

> 依据：2026-08-27 通读源码 + 与 Claude Code / Codex / OpenCode / DeepSeek Harness 的能力与交互对比。
> 约束：不引入 agent 框架/SDK、不使用托管执行/文件工具，全部逻辑自研；仅用模型厂商客户端 + 原生 tool calling + OpenAI 兼容网关。

## 0. 一句话结论

SEECODER **缺的不是`能否调用工具`，而是`人机协同的表层`与`记忆/检索的深度`**。
它现在是一个**单任务、自动执行、无记忆**的`工程 Agent`，而 Claude Code / Codex / OpenCode 是**多轮会话、可审批、可计划、可记忆检索**的`产品化 Agent`。补齐这两层，就从`演示用 agent`变成`可用的 coding agent`。

## 1. 主流 Agent 能力全景（SEECODER 现状打勾）

| 能力维度 | Claude Code / Codex / OpenCode 典型形态 | SEECODER 现状 |
| --- | --- | --- |
| 交互形态 | 交互式多轮会话（REPL/TUI/桌面聊天） | ❌ 单任务 CLI（一次 `run`） |
| 工作模式 | plan / ask / auto / acceptEdits / dontAsk / bypassPermissions | ❌ 无模式概念（默认全自动） |
| 人机协同审批 | 编辑文件/运行命令前暂停并请求批准、可查看 diff | ❌ 无审批门，受限模式下自动执行 |
| 上下文管理 | tokenizer 感知 + 自动压缩/摘要 + 结构化注入（文件树/计划） | ◐ 确定性字符预算裁剪（无摘要） |
| 持久记忆 | AGENTS.md / CLAUDE.md，跨会话复用 | ❌ 无记忆文件 |
| 代码检索 | 符号/语义/嵌入索引（search_code/索引工具） | ◐ 仅有精确字符串 `search_files` |
| 会话持久/恢复 | 会话可保存、checkpoint、resume | ◐ 桌面存了消息，后端无会话/无 checkpoint |
| 并行工具调用 | 独立调用可并行 | ❌ 串行（`for call in tool_calls`） |
| 子代理/分诊 | orchestrator + reviewer/implementer 子代理 | ❌ 无 |
| 流式输出 | token 流式、增量渲染 | ❌ 非流式（`create` 无 stream） |
| 成本/用量 | 每次运行显示 token 与成本 | ❌ 无 usage 收集 |
| 多模型/路由 | 模型选择器、跨模型路由 | ◐ 单 provider 适配（接口可扩展） |
| 更强的执行隔离 | 容器/VM/网络隔离沙箱 | ❌ restricted argv 白名单；文档明示`非 OS 沙箱` |
| web 检索 / vision | 联网搜索、图像输入 | ❌ 无 |
| 扩展机制 | 插件 / MCP | ❌ 无 |

## 2. 两张截图对照（Codex 桌面 vs SEECODER 桌面）

**Codex 桌面截图**（具备而 SEECODER 缺的交互元素）：
- 侧栏项目/会话列表（多个项目、多会话、`最近`）。
- 顶部工作流入口：**新对话 / 拉取请求 / 已安排 / 插件**。
- 欢迎页**起始建议卡片**：探索并理解代码 / 构建新功能、应用或工具 / 审查代码并提出修改建议 / 修复问题和失败。
- 底部**上下文栏**：当前项目 | 仓库/本地 | 当前分支（main）。
- **审批控件**：`+ 帮我批准`——把`执行前需人确认`直接做进 UI。
- **模型/成本指示**：如 `5.6 Luna`（用量/模型调用指示）。

**SEECODER 桌面截图现状**：项目会话侧栏、工作区选择、对话、运行轨迹、停止、关于对话框、本地优先标识。已具备`会话 + 轨迹 + 停止`骨架，但缺乏：起始建议卡片、上下文栏（branch/模型/成本）、审批门、成本显示、模型选择。

## 3. 能力差距清单（按对`真 Agent`的重要性分级）

### T1 关键缺失——决定它`像不像`一个 Agent 产品（视频里最能加分）
1. **交互式多轮会话**：`run` 单任务 -> `Conversation` 对象，支持多轮追问、改任务、续上轮。
2. **计划/询问/自动三模式**：plan（先出方案不执行，批了再干）、ask（先澄清）、auto（现状）。
3. **人机协同审批门**：写文件/打补丁/跑命令前弹出 diff 与批准/拒绝；支持`本会话此组操作不再询问`（acceptEdits 语义）；desktop 加`批准`按钮。
4. **流式输出 + 成本/用量**：`stream=True` 增量渲染；解析 `usage` 并累计，desktop 徽章显示 token/成本。
5. **上下文自动压缩/摘要 + 持久记忆**：预算触底时用一次模型调用把旧回合压缩成`工作记忆`注入；读取/写入项目级 `SEECODER.md`（/ AGENTS.md）作为跨会话记忆。

### T2 深度缺失——提升真实任务成功率
6. **代码库索引/语义检索**：自研符号/索引（可选嵌入），提供 `search_code` 工具，优于现有字符串搜索。
7. **并行工具调用**：只读且无依赖的调用并发执行；变更操作保持串行。
8. **会话恢复/检查点**：运行前快照 workspace 与对话，失败/中断可回滚（Claude Code checkpoint 语义）。
9. **子代理/分诊循环**：orchestrator 派发 review/implement 子任务，自研 fan-out。

### T3 增强/拉伸（可选）
10. `web_search` 工具、图像输入。
11. 更强执行隔离（macOS `sandbox-exec` / 临时工作副本）；**不要**把受限模式宣传成沙箱。
12. 多模型路由 + desktop 模型选择器。
13. 插件/MCP 兼容注册（自研）。

## 4. 架构方案（新增模块，全部自研，对现有代码最小侵入）

```
新增：src/seecoder/
  session.py     # Conversation：多轮状态、历史持久化/恢复、checkpoint
  plan.py        # plan/ask/auto 模式门控：是否执行工具、是否先出方案
  approval.py    # 审批门：mutate 工具前的 diff 预览 + 批准/拒绝 + 权限规则
  usage.py       # 解析 usage、累计 token/成本、写入 trace/desktop 徽章
  compaction.py  # 预算触底的自动摘要（一次模型调用）→ 工作记忆注入
  memory.py      # 项目级 SEECODER.md/AGENTS.md 读写与上下文注入
  streaming.py   # 流式事件解析（或并入 model_client）
  index.py       # 代码库符号/语义索引 + search_code 工具（可选）
  subagents.py   # orchestrator + 子任务 fan-out（自研）
  sandbox.py     # 可选：更严执行隔离

改造现有：
  runner.py      # run(task) -> Conversation; for call 串行 -> 并行(只读); 插入 approval 回调
  model_client.py# 增加 complete_stream + usage 收集; 保留现有非流式接口
  context.py     # 字符预算 -> tokenizer 感知估计 + compaction 策略
  types.py       # 增加 ConversationState / ApprovalRequest / Usage / Mode
  cli.py         # 增加 `chat` 交互子命令; `run` 增加 --mode / --approve
  tools/         # 增加 search_code / web_search / git_log / memory 读写
  desktop/electron/renderer + main.cjs  # 建议卡片 / 上下文栏 / 审批按钮 / 成本徽章 / 会话持久化
```

## 5. 分阶段路线图（时间盒，契合 6 天 deadline）

> 优先级铁律：**先保住提交物，再谈特性。** 任何阶段都不能挤掉 README.txt / 视频 / 预演。

> **进度（2026-08-27）**：阶段 1（交互会话 / 三模式 / 审批门 / 成本 + 桌面建议卡片）✅ 已提交；阶段 2（流式 / 上下文压缩 / 记忆文件 / 会话持久化恢复）✅ 已完成并真实联调验证；阶段 0 提交物与阶段 3/4 待做。

- **阶段 0 · 必做（0.5 天）——先锁定交付**：三次连续预演（从原始失败 fixture 复制新 workspace，保持源 fixture 永远失败）；起草 README.txt（≤1000 汉字）；写视频脚本；打包 `姓名.zip`（仅 README.txt + 视频）。此阶段完成即有可提交物，后面所有特性都是`加分`。
- **阶段 1 · `像 Agent`（1–1.5 天）——最高杠杆**：交互会话 + plan/ask/auto 三模式 + 审批门 + 成本/用量；desktop 加起始建议卡片、上下文栏（branch/模型/成本）、`批准`按钮。**这是视频里最能展示`真 Agent`的部分**。
- **阶段 2 · `能产出`（1.5–2 天）**：流式 + 上下文压缩/摘要 + 记忆文件（SEECODER.md）+ 会话持久化/恢复/检查点。
- **阶段 3 · `更聪明`（可选 1–2 天）**：代码索引语义检索 + 并行工具调用 + web_search + 子代理（review/implement 循环）。
- **阶段 4 · 保障（0.5 天）**：更强沙箱（可选）+ 全量离线测试 + 干净环境回归（注意 `Settings` 进程环境优先语义）+ 三次预演 + 最终公开仓库检查。
- **纪律**：每个阶段小步提交、补离线测试、真实 API 受控验证、更新 docs；9/2 24:00 后停止推送。

## 6. 边界与答辩要点（务必守住）

- **所有新增逻辑均为自研**，继续只依赖 `openai` 客户端 + 原生 tool calling，符合红线。
- **审批门是最`像真 Agent`也最能体现`你理解为何这样运转`的点**——答辩重点：你要能解释为什么需要 plan/审批/成本，以及权限模式如何权衡监督与效率。
- **不要把 restricted 模式说成沙箱**；阶段 4 若升级隔离，要说明其局限，避免被问倒。
- **摘要记忆是本地逻辑**（额外一次模型调用），允许；但要写清何时触发、如何兜底，别破坏`确定性上下文`的既有承诺。
- **子代理是自研 fan-out**（仿 Claude Code subagents / OpenCode agents），不在禁用清单，可放心做。
- **提交物红线**：API key 不进入 README.txt/视频/仓库；录屏避免出现 `.env`；截止后不推送；历史不压缩改写。

## 7. 建议的下一步

若按`6 天最大化收益`排序，我建议先做 **阶段 0（锁定提交物）→ 阶段 1 的`审批门 + 三模式 + 交互会话 + 桌面建议卡片`**。这是投入产出比最高、也最能支撑面试`你理解 agent 为何这样运转`的一组能力。你可指定从哪一项开始，我会给出具体实现（模块划分、代码接缝、离线测试）并在真实 API 下做受控验证。
