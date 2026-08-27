# P3 Phase-1：交互会话 + 三模式 + 审批门 + 成本（已实现并验证）

> 完成日期：2026-08-27。目标是把 SEECODER 从`单任务、自动执行`升级为`可交互、可审批、可计划`的 coding agent。全部逻辑自研，未引入任何 Agent 框架/SDK；仅用 `openai` 客户端 + 原生 tool calling。

## 1. 新增/修改模块

| 模块 | 内容 |
| --- | --- |
| `types.py` | 新增 `Mode`(auto/plan/ask)、`ApprovalDecision`、`Usage`、`PlanStep`、`RunState.PLAN_PROPOSED`；扩展 `ModelResponse.usage` 与 `RunOutcome.plan/mode/usage` |
| `approval.py` | `is_read_only()` 工具读写分类 + `Policy`（plan/ask/auto 权限决策） |
| `usage.py` | `UsageTracker` 累计 token 用量 |
| `runner.py` | 主循环接入模式与审批门：plan 模式把改写/命令捕获为计划、ask 模式逐动作审批、auto 保持原行为；非流式下累计 usage 并写入 trace/事件 |
| `session.py` | `Conversation` 交互式多轮会话：start / send / approve_plan / summary，累积 usage 与步数 |
| `model_client.py` | 解析并返回 `usage` |
| `config.py` | `Settings.mode` + `SEECODER_MODE` 校验 |
| `cli.py` | `run` 加 `--mode`/`--auto-approve`；新增交互式 `chat` 子命令；stdin 审批协议；新增状态退出码 |

## 2. 设计要点

- **三模式语义**：auto = 受限允许的工具直接执行（向后兼容）；plan = 只读工具可跑，改写/命令不执行而是作为`计划`产出（`PLAN_PROPOSED`），供人审查后以自动模式执行；ask = 每个改写/命令在调用前暂停，等待批准（stdin/桌面按钮）。
- **审批门**：`Policy` 把非只读工具在 ask 模式下判定为 `NEEDS_APPROVAL`，runner 在真正 dispatch 前回调 approver；批准则执行、拒绝则回填 `DeniedByUser` 工具错误，模型据此调整。同时发出 `approval_request` / `plan_proposal` 事件给桌面端。
- **plan 模式不破坏确定性**：计划步骤单独收集，不作为连续工具错误累加；读到的信息仍回填给模型，确保它能给出文本计划。
- **成本**：每次模型响应解析 `usage`，累计后写入 trace 并发出 `usage` 事件；CLI 与桌面端都可显示 token 用量。

## 3. 离线测试（干净环境全绿）

新增长 `test_modes.py`（策略 + plan/ask/auto 行为 + usage）与 `test_session.py`（多轮会话 + 计划审批），并给 `test_model_client.py` 补 usage 解析用例。
结果：`54 项`全部通过（原 43 + 新 11）。Electron 核心测试 `3 项`通过。

## 4. 真实 API 受控验证（deepseek-v4-flash）

1. **连通性**：`run` 一次无工具请求，返回 `OK`；usage 显示约 1172 tokens。
2. **plan 模式闭环**：对 demo_workspace 执行`仅诊断并给方案`。结果：只读检查，定位 `normalize_tag` 缺陷，给出 `tag.strip().lower()` 的精确修复与验证命令；`run_command` 被捕获为计划步骤；`未修改任何文件`（git status 干净）；usage 约 9123 tokens。
3. **ask 模式（stdin 关闭=拒绝）**：模型尝试 patch 与 run_command，均触发审批并被拒；随后给出清晰说明、优雅退出、无挂起；usage 约 12419 tokens。

## 5. 桌面端（Electron）新增

- 起始页**建议卡片**（探索/构建/审查/修复，预填任务）。
- 底部**工作模式选择**（询问/计划/自动）。
- **成本徽章**（显示累计 token）。
- **审批横幅**（批准/拒绝按钮，经 stdin 协议回写子进程）。
- **计划批准重跑**（plan_proposed 后以自动模式重新执行）。

## 6. 仍未做（见 roadmap P3 后续）

流式渲染、上下文自动摘要/记忆文件、会话持久化/检查点、代码索引语义检索、并行工具调用、web_search、子代理、更强执行隔离。
